"""Build the fixed synthetic Chinese corpus and its evidence-labelled question set."""

import json
from pathlib import Path


SYSTEMS = [
    ("alder", "赤杨", "订单归档", "归档积压", 720, 11, "西区备用队列", "archive", "批量重放", "订单摘要"),
    ("birch", "白桦", "发票导出", "导出停滞", 540, 14, "二号导出队列", "export", "恢复导出", "税务凭证"),
    ("cedar", "雪松", "库存同步", "库存偏差", 840, 16, "库存修复队列", "stock", "强制对账", "库存快照"),
    ("elm", "青榆", "通知投递", "通知延迟", 660, 18, "通知缓冲队列", "notify", "重新投递", "消息回执"),
    ("fir", "银杉", "账单汇总", "账单缺项", 960, 21, "月结修复队列", "billing", "重跑月结", "账期索引"),
    ("ginkgo", "银杏", "权限刷新", "授权不一致", 780, 23, "权限刷新队列", "access", "重建权限", "授权缓存"),
    ("larch", "落叶松", "搜索更新", "索引滞后", 1020, 26, "索引补偿队列", "index", "补建索引", "查询字典"),
    ("maple", "红枫", "文件扫描", "扫描积压", 900, 28, "离线扫描队列", "scan", "补扫文件", "风险样本"),
    ("pine", "青松", "会话清理", "会话残留", 1140, 31, "清理缓冲队列", "purge", "重新清理", "访问记录"),
    ("willow", "垂柳", "指标聚合", "聚合缺点", 1080, 33, "指标回填队列", "metric", "补算指标", "采样窗口"),
]

SECTIONS = [
    ("用途与范围", "本手册用于轮值交接。跨服务问题交给当前值班协调人分派，运行人员先确认环境，再选择操作。"),
    ("权限申请", "值班人员通过团队工单系统申请临时角色。审批完成后应核对账号所在环境和租户。"),
    ("工单模板", "工单应包含发现时间、影响范围、相关变更记录、观测窗口和复现步骤。"),
    ("周会复盘", "会前收集本周变更单号和回滚结论，逐一确认待办项已经分派到责任人。"),
    ("容量巡检", "先比较最近两周的队列趋势，再查看 CPU、磁盘和网络图表是否同时出现异常。"),
    ("数据保留", "导出排障附件前，先检查数据分类并移除与当前故障无关的样例记录。"),
    ("压测安排", "压测申请需要记录预计持续时间、并发范围以及结束后的恢复动作。"),
    ("版本发布", "发布单需注明验收负责人和发布窗口。新旧版本同时运行时分别记录观察结果。"),
    ("应急升级", "影响范围扩大后，值班人员更新事件等级和沟通频率，保留原始时间线。"),
    ("日志定位", "优先根据请求编号定位相关日志，再结合应用实例和部署批次缩小范围。"),
    ("告警订阅", "变更告警接收人后，轮值同事通过测试告警确认新规则能正常送达。"),
    ("值班演练", "演练记录应区分预期现象和实际现象，记录后续验证人及验证时间。"),
    ("依赖变更", "外部依赖的计划变更统一登记，执行前检查回退联系人和变更窗口。"),
    ("工单关闭", "关闭工单前复核影响范围与恢复时间，填写复发监控指标和确认人。"),
    ("故障通报", "首次通报给出受影响功能和下一次更新时间，后续版本同步记录变化。"),
    ("附录索引", "本手册的操作位置以二级标题为准，线下演练记录保存在独立工单附件中。"),
]


def main():
    root = Path(__file__).resolve().parent
    documents = root / "corpus"
    documents.mkdir(exist_ok=True)
    questions = []

    for slug, name, domain, symptom, wait, retry, queue, command, action, item in SYSTEMS:
        a, b, c = f"{slug}-prod", f"{slug}-stage", f"{slug}-guide"
        prod, stage = f"{name}生产环境", f"{name}预发环境"
        code_a, code_b = f"{slug.upper()}-R17", f"{slug.upper()}-R71"
        check_a, check_b = f"{command}-verify --digest", f"{command}-verify --schema"
        lines = {
            "a_code": (a, f"{prod}发生{symptom}时，告警代码为 {code_a}。"),
            "a_wait": (a, f"{prod}的 job_wait_ms 默认值为 {wait} 毫秒。"),
            "a_retry": (a, f"{prod}最多自动重试 {retry} 次。"),
            "b_code": (b, f"{stage}发生{symptom}时，告警代码为 {code_b}。"),
            "b_wait": (b, f"{stage}的 job_wait_ms 默认值为 {wait + 250} 毫秒。"),
            "b_retry": (b, f"{stage}最多自动重试 {retry + 7} 次。"),
            "g_queue": (c, f"{prod}{action}失败时，先把待处理任务转到{queue}。"),
            "g_prod": (c, f"{prod}切换后必须运行 `{check_a}` 校验{item}。"),
            "g_stage": (c, f"{stage}演练切换后运行 `{check_b}` 校验{item}。"),
        }
        for doc, title, keys in [
            (a, f"{prod}参数表", ("a_code", "a_wait", "a_retry")),
            (b, f"{stage}参数表", ("b_code", "b_wait", "b_retry")),
        ]:
            body = "\n\n".join(lines[key][1] for key in keys)
            (documents / f"{doc}.md").write_text(
                f"# {title}\n\n负责{domain}。以下记录仅适用于标题所列环境。\n\n{body}\n",
                encoding="utf-8",
            )
        sections = [f"# {name}{domain}值班手册"]
        for i, (heading, paragraph) in enumerate(SECTIONS):
            sections.append(f"## {heading}\n\n{paragraph}")
            if i == 5:
                sections.append(f"## 生产异常分流\n\n{lines['g_queue'][1]}")
            if i == 10:
                sections.append(f"## 预发切换演练\n\n{lines['g_stage'][1]}")
            if i == 13:
                sections.append(f"## 生产切换验证\n\n{lines['g_prod'][1]}")
        (documents / f"{c}.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")

        rows = [
            ("exact", f"{code_a} 对应什么告警？", ("a_code",)),
            ("exact", f"{code_b} 在哪种环境出现，指的是什么？", ("b_code",)),
            ("paraphrase", f"{name}正式上线的服务遇到失败，系统自己最多能再试几回？", ("a_retry",)),
            ("paraphrase", f"{name}正式线上{action}没成功，未做完的活儿应该先挪到哪里？", ("g_queue",)),
            ("confusable", f"{name}只看生产，job_wait_ms 的默认值是多少？", ("a_wait",)),
            ("confusable", f"{name}只看预发，job_wait_ms 的默认值是多少？", ("b_wait",)),
            ("long_doc", f"{name}切换生产环境后，用哪条命令检查{item}？", ("g_prod",)),
            ("long_doc", f"{name}切换预发环境做演练后，用哪条命令检查{item}？", ("g_stage",)),
            ("multi_evidence", f"{name}生产环境最多自动试几次？{action}失败后应转到哪个队列？", ("a_retry", "g_queue")),
            ("multi_evidence", f"{name}预发环境的默认等待时间是多少？预发切换后用什么命令校验？", ("b_wait", "g_stage")),
            ("no_answer", f"{name}生产环境的 job_wait_ms 最大可配置值是多少？", ()),
            ("no_answer", f"{name}预发环境演练后，{item}的校验结果保留多少天？", ()),
        ]
        for i, (kind, query, keys) in enumerate(rows, 1):
            evidence = [dict(id=f"{slug}-{key}", document_id=lines[key][0], quote=lines[key][1]) for key in keys]
            questions.append(dict(id=f"{slug}-{i:02}", category=kind, query=query, evidence=evidence))

    with (root / "questions.jsonl").open("w", encoding="utf-8") as output:
        for question in questions:
            output.write(json.dumps(question, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
