"""Restore a private, independently owned index clone for formal RRF verification.

Run only after the existing 100k benchmark has a complete preserved checkpoint.
No original database or deployment file is modified. Credentials stay outside git.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

from run_scale import save


def main(args):
    repo = Path(__file__).resolve().parents[1]
    work = args.work.resolve()
    if work.is_relative_to(repo):
        raise ValueError('The private runtime must be outside the benchmark repository')
    if not re.fullmatch(r'miracl_rrf_config_validation_[a-z0-9_]+', args.database):
        raise ValueError('Use a dedicated database name beginning miracl_rrf_config_validation_')
    settings = json.loads(args.settings.read_text())
    original = Path(settings['work']) / 'runtime-v2/contextbase-100k'
    backup = args.backup.resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = work / 'restore-owner.json'
    complete = work / 'restored.json'
    if complete.exists():
        proof = json.loads(complete.read_text())
        if proof['database'] != args.database or proof['backup'] != str(backup) or proof['bytes'] != backup.stat().st_size:
            raise ValueError('Existing restore marker belongs to another database or backup')
        print('Verified existing private restore marker; no restore or file overwrite performed')
        return
    connection = psycopg2.connect(settings['postgres_url'])
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (args.database,))
            exists = cursor.fetchone() is not None
            with backup.open('rb') as stream:
                backup_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
            expected_owner = {'database': args.database, 'backup_sha256': backup_sha256}
            if exists and (not owner.exists() or json.loads(owner.read_text()) != expected_owner):
                raise ValueError('Refusing to overwrite an existing database without this private ownership marker')
            if not exists:
                cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(args.database)))
            save(owner, expected_owner)
    finally:
        connection.close()
    # --clean touches only the independently owned clone. Serial maintenance
    # avoids pgvector allocating >1 GB in Docker's commonly small /dev/shm.
    with backup.open('rb') as stream:
        subprocess.run(['docker', 'exec', '-i', '-e', 'PGOPTIONS=-c max_parallel_maintenance_workers=0 -c maintenance_work_mem=1GB',
                        args.container, 'pg_restore', '-U', 'postgres', '-d', args.database,
                        '--clean', '--if-exists', '--no-owner', '--no-privileges', '--exit-on-error'], stdin=stream, check=True)
    profile = work / 'runtime'
    profile.mkdir(mode=0o700, exist_ok=True)
    database_url = urlunsplit(urlsplit(settings['postgres_url'])._replace(path='/' + args.database))
    for name in ('admin-key.json', 'api-secrets.json', 'ui-secrets.json', 'api.env', 'ingest.sqlite', 'ingest.timing.jsonl'):
        target = profile / name
        if not target.exists():
            shutil.copy2(original / name, target)
            target.chmod(0o600)
    env_file = profile / 'api.env'
    lines = []
    for line in env_file.read_text().splitlines():
        if '=' in line and line.split('=', 1)[0] in ('CONTEXTBASE_API_DATABASE_URL', 'HINDSIGHT_API_DATABASE_URL'):
            line = line.split('=', 1)[0] + '=' + shlex.quote(database_url)
        lines.append(line.replace(str(original), str(profile)).replace(settings['contextbase_source'], str(args.source.resolve())))
    env_file.write_text('\n'.join(lines) + '\n')
    env_file.chmod(0o600)
    (work / 'database-url').write_text(database_url)
    (work / 'database-url').chmod(0o600)
    save(complete, {'database': args.database, 'backup': str(backup), 'bytes': backup.stat().st_size})
    print('Independent index clone ready; original service data untouched')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--database', default='miracl_rrf_config_validation_20261113')
    parser.add_argument('--container', default='miracl-pg')
    main(parser.parse_args())
