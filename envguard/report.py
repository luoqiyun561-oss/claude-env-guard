"""Report serialization: JSON for tooling, Markdown and plain text for people.

Three formats because they answer three different questions: JSON is the record
(and the only one that keeps every measured fact), Markdown is what goes into a
file next to the machine, and plain text is what gets pasted into a chat. All
three are generated from the same `Report`, so a reader can never see a verdict
in one format that the others do not carry.
"""

import json

from .checks import Report

_STATUS_TEXT = {"good": "通过", "warn": "注意", "bad": "风险", "unknown": "未检测"}


def to_dict(report: Report) -> dict:
    score, earned, total = report.total()
    return {
        "generated_at": report.generated_at,
        "verdict": report.verdict(),
        "score": round(score, 1),
        "weight_earned": earned,
        "weight_total": total,
        "factors": {
            factor: dict(zip(("score", "earned", "total"), _fmt_factor(report, factor)))
            for factor in ("network", "device", "behavior")
        },
        "checks": [
            {
                "id": c.id,
                "title": c.title,
                "factor": c.factor,
                "weight": c.weight,
                "status": c.status,
                "detail": c.detail,
                "advice": c.advice,
            }
            for c in report.checks
        ],
        "facts": report.facts,
    }


def _fmt_factor(report: Report, factor: str) -> tuple[float, int, int]:
    score, earned, total = report.factor_score(factor)
    return round(score, 1), earned, total


def to_json(report: Report) -> str:
    return json.dumps(to_dict(report), ensure_ascii=False, indent=2)


def to_markdown(report: Report) -> str:
    score, earned, total = report.total()
    lines = [
        "# Claude 环境自检报告",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 总分：**{score:.0f}** / 100（{earned}/{total} 分权重）",
        "",
        "## 分项",
        "",
        "| 因素 | 得分 | 权重 |",
        "| --- | --- | --- |",
    ]
    for factor, label in (("network", "网络"), ("device", "设备"), ("behavior", "使用")):
        factor_score, factor_earned, factor_total = report.factor_score(factor)
        cell = f"{factor_score:.0f}" if factor_total else "—"
        lines.append(f"| {label} | {cell} | {factor_earned}/{factor_total} |")

    lines += ["", "## 检查项", ""]
    for check in report.checks:
        lines.append(f"### {_STATUS_TEXT[check.status]}｜{check.title}（权重 {check.weight}）")
        lines.append("")
        lines.append(check.detail)
        if check.advice:
            lines.append("")
            lines.append(f"> {check.advice}")
        lines.append("")

    if report.facts:
        lines += ["## 原始数据", "", "```json", json.dumps(report.facts, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def to_text(report: Report) -> str:
    score, earned, total = report.total()
    lines = [
        f"Claude 环境自检　{report.generated_at}",
        f"总分 {score:.0f}/100（{earned}/{total} 分权重）",
        "",
    ]
    for check in report.checks:
        lines.append(f"[{_STATUS_TEXT[check.status]}] {check.title}（权重 {check.weight}）")
        lines.append(f"    {check.detail}")
        if check.advice:
            lines.append(f"    → {check.advice}")
    return "\n".join(lines)
