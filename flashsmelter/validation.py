"""请求参数的一次性聚合校验。

组件里的 :class:`~flashsmelter.params.Params` 是「取一个、验一个、错即抛」，
值班发一条指令只能撞到第一个问题。这里改为按动作规格
（:mod:`~flashsmelter.specs`）把整份请求一次验完：

- 缺了哪些必填参数——逐个列名，不再一次只说一个；
- 越了哪条线——给出参数名、实际值与允许范围（含单位）；
- 哪个参数根本不认——点名未知参数，并对常见笔误给出「你是不是想写」；
- 单位疑似填错——按参数单位反查常见的错单位换算（Pa 当成 kPa、百分数当成
  比例等），在同一条问题里给出换算提示。

校验只看请求形状，不碰炉况；门控类失败仍由组件抛 ``guard-violation``。
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationError
from .specs import BOOLEAN, INTEGER, NUMBER, TEXT, ActionSpec, ParamSpec

_TRUE_LITERALS = {"true", "1", "yes", "on"}
_FALSE_LITERALS = {"false", "0", "no", "off"}

# code 取值保持稳定，审计与值班脚本可据此分类。
MISSING = "missing"
UNKNOWN = "unknown-param"
BAD_TYPE = "invalid-type"
OUT_OF_RANGE = "out-of-range"
BAD_CHOICE = "invalid-choice"
BLANK = "blank"
TOO_LONG = "too-long"

# 每种单位下值班最容易填错的「另一个单位」：factor 为把填错的值换算回规定单位
# 的倍数。值越界且换算后正好落进允许范围，就基本可以断定是单位问题。
UNIT_ALIASES: dict[str, tuple[tuple[str, float, str], ...]] = {
    "kPa": (
        ("Pa", 0.001, "1 kPa = 1000 Pa"),
        ("MPa", 1000.0, "1 MPa = 1000 kPa"),
        ("bar", 100.0, "1 bar = 100 kPa"),
    ),
    "Nm³/h": (
        ("m³/h", 1.0, "Nm³/h 与 m³/h 数值口径不同，请按标况 Nm³/h 填报"),
    ),
    "t/h": (
        ("kg/h", 0.001, "1 t/h = 1000 kg/h"),
        ("t/min", 60.0, "1 t/min = 60 t/h"),
    ),
    "t": (
        ("kg", 0.001, "1 t = 1000 kg"),
    ),
    "m": (
        ("mm", 0.001, "1 m = 1000 mm"),
        ("cm", 0.01, "1 m = 100 cm"),
    ),
    "s": (
        ("ms", 0.001, "1 s = 1000 ms"),
        ("min", 60.0, "1 min = 60 s"),
        ("h", 3600.0, "1 h = 3600 s"),
    ),
    "℃": (
        ("K", None, "℃ 与 K 不是线性换算，请直接填报摄氏温度"),
        ("℉", None, "请直接填报摄氏温度 ℃"),
    ),
    "比例(0~1)": (
        ("百分数(0~100)", 0.01, "比例要填小数：62% 应填 0.62"),
    ),
}

_SUGGEST_CUTOFF = 0.6


@dataclass(slots=True)
class Issue:
    """一条参数问题。"""

    code: str
    param: str
    message: str
    unit: str | None = None
    value: Any = None
    minimum: Any = None
    maximum: Any = None
    exclusive_minimum: bool = False
    choices: list[str] | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "param": self.param, "message": self.message}
        if self.unit is not None:
            payload["unit"] = self.unit
        if self.value is not None or self.code in (OUT_OF_RANGE, BAD_TYPE):
            payload["value"] = self.value
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.exclusive_minimum:
            payload["exclusive_minimum"] = True
        if self.choices is not None:
            payload["choices"] = self.choices
        if self.suggestion is not None:
            payload["suggestion"] = self.suggestion
        return payload


def _format_number(value: float) -> str:
    if isinstance(value, bool):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def format_range(spec: ParamSpec) -> str:
    """把允许范围翻成值班一眼能懂的区间串。"""

    lo = spec.minimum
    hi = spec.maximum
    unit = spec.unit
    if lo is None and hi is None:
        return f"单位 {unit}" if unit else "无取值限制"
    if lo is not None and hi is not None:
        text = f"{_format_number(lo)}~{_format_number(hi)}"
    elif hi is not None:
        text = f"不超过 {_format_number(hi)}"
    else:
        comparator = "大于 " if spec.exclusive_minimum else "不小于 "
        text = comparator + _format_number(lo)
    return f"{text} {unit}" if unit else text


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _unit_hint(spec: ParamSpec, number: float) -> str | None:
    """越界值疑似单位填错时，给出换算提示。"""

    aliases = UNIT_ALIASES.get(spec.unit or "")
    if not aliases:
        return None
    lo = spec.minimum
    hi = spec.maximum
    for alias, factor, note in aliases:
        if factor is None:
            continue
        converted = number * factor
        in_range = (lo is None or converted >= lo) and (hi is None or converted <= hi)
        if not in_range:
            continue
        return (
            f"当前值按 {alias} 填报换算为 {_format_number(converted)} 后落在允许范围内——"
            f"疑似单位填错，{note}。请核对后按 {spec.unit} 重新填报。"
        )
    # 百分数/比例这类最常见错误：值大于 1 而参数要求 0~1，给出明确指引。
    if spec.unit == "比例(0~1)" and number > 1:
        converted = number / 100.0
        if (lo is None or converted >= lo) and (hi is None or converted <= hi):
            return f"该参数是 0~1 的比例，不是百分数：{_format_number(number)} 若是百分数，应填 {converted:g}（如 62% 填 0.62）。"
    return None


def validate_action(spec: ActionSpec, raw: Mapping[str, Any], *, source: str = "request") -> None:
    """按规格校验整份请求；有任何问题都收齐后一次性抛出。"""

    issues: list[Issue] = []
    known = spec.names()

    for name in sorted(set(raw) - set(known)):
        suggestion = None
        candidates = difflib.get_close_matches(name, known, n=1, cutoff=_SUGGEST_CUTOFF)
        message = f"参数 {name} 不被动作 {spec.action} 识别"
        if candidates:
            suggestion = candidates[0]
            message += f"，是不是想写 {suggestion}？"
        else:
            message += f"，该动作可用参数：{', '.join(known)}"
        issues.append(
            Issue(
                UNKNOWN,
                name,
                message,
                suggestion=suggestion,
                value=_safe_repr(raw.get(name)),
            )
        )

    for pspec in spec.params:
        present = pspec.name in raw
        value = raw.get(pspec.name)
        if not present or value is None or (isinstance(value, str) and not value.strip()):
            if pspec.required:
                issues.append(
                    Issue(
                        MISSING,
                        pspec.name,
                        f"缺少必填参数 {pspec.name}（{pspec.label}）"
                        + (f"，单位 {pspec.unit}" if pspec.unit else ""),
                        unit=pspec.unit,
                    )
                )
            continue
        issues.extend(_check_value(pspec, value))

    if issues:
        raise _build_error(spec, issues, source)


def _check_value(spec: ParamSpec, value: Any) -> list[Issue]:
    if spec.kind == TEXT:
        return _check_text(spec, value)
    if spec.kind == BOOLEAN:
        return _check_boolean(spec, value)
    return _check_number(spec, value)


def _check_text(spec: ParamSpec, value: Any) -> list[Issue]:
    if not isinstance(value, str):
        return [
            Issue(
                BAD_TYPE,
                spec.name,
                f"参数 {spec.name}（{spec.label}）必须是文本，收到 {type(value).__name__}",
                value=_safe_repr(value),
            )
        ]
    text = value.strip()
    if not text:
        return [Issue(BLANK, spec.name, f"参数 {spec.name}（{spec.label}）不能为空白文本")]
    if len(text) > spec.max_length:
        return [
            Issue(
                TOO_LONG,
                spec.name,
                f"参数 {spec.name}（{spec.label}）长度 {len(text)} 超过上限 {spec.max_length} 个字符",
                value=text,
                maximum=spec.max_length,
            )
        ]
    if spec.choices is not None and text not in spec.choices:
        return [
            Issue(
                BAD_CHOICE,
                spec.name,
                f"参数 {spec.name}（{spec.label}）取值不被接受：{text}，允许取值：{', '.join(spec.choices)}",
                value=text,
                choices=list(spec.choices),
            )
        ]
    return []


def _check_boolean(spec: ParamSpec, value: Any) -> list[Issue]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int) and value in (0, 1):
        return []
    text = str(value).strip().lower() if isinstance(value, str) else None
    if text in _TRUE_LITERALS or text in _FALSE_LITERALS:
        return []
    accepted = "true/false、1/0、yes/no、on/off"
    return [
        Issue(
            BAD_TYPE,
            spec.name,
            f"参数 {spec.name}（{spec.label}）必须是布尔值，收到 {_safe_repr(value)}；可填 {accepted}",
            value=_safe_repr(value),
        )
    ]


def _check_number(spec: ParamSpec, value: Any) -> list[Issue]:
    number = _to_number(value)
    if number is None:
        return [
            Issue(
                BAD_TYPE,
                spec.name,
                f"参数 {spec.name}（{spec.label}）必须是数值"
                + (f"（单位 {spec.unit}）" if spec.unit else ""),
                unit=spec.unit,
                value=_safe_repr(value),
            )
        ]
    if math.isnan(number) or math.isinf(number):
        return [
            Issue(
                BAD_TYPE,
                spec.name,
                f"参数 {spec.name}（{spec.label}）必须是有限数值，不能是 {number}",
                unit=spec.unit,
                value=_safe_repr(value),
            )
        ]
    if spec.kind == INTEGER and abs(number - round(number)) > 1e-9:
        return [
            Issue(
                BAD_TYPE,
                spec.name,
                f"参数 {spec.name}（{spec.label}）必须是整数，收到 {_format_number(number)}",
                unit=spec.unit,
                value=number,
            )
        ]

    lo = spec.minimum
    hi = spec.maximum
    below = lo is not None and (number <= lo if spec.exclusive_minimum else number < lo)
    above = hi is not None and number > hi
    if not below and not above:
        return []

    comparator = "低于下限" if below else "高于上限"
    message = (
        f"参数 {spec.name}（{spec.label}）={_format_number(number)} {comparator}，"
        f"允许范围：{format_range(spec)}"
    )
    hint = _unit_hint(spec, number)
    if hint is not None:
        message += f"。{hint}"
    return [
        Issue(
            OUT_OF_RANGE,
            spec.name,
            message,
            unit=spec.unit,
            value=number,
            minimum=lo,
            maximum=hi,
            exclusive_minimum=spec.exclusive_minimum,
            suggestion=hint,
        )
    ]


def _safe_repr(value: Any) -> Any:
    """放进 JSON 错误详情的值；不可序列化的退化为字符串。"""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _build_error(spec: ActionSpec, issues: list[Issue], source: str) -> ValidationError:
    count = len(issues)
    if count == 1:
        summary = f"指令 {spec.action} 参数校验未通过：{issues[0].message}"
    else:
        summary = f"指令 {spec.action} 有 {count} 个参数问题，请逐条修正后重发"
    return ValidationError(
        summary,
        details={
            "source": source,
            "action": spec.action,
            "issue_count": count,
            "param": issues[0].param,  # 兼容只认 details.param 的旧调用方
            "issues": [item.to_dict() for item in issues],
        },
    )


__all__ = ["validate_action", "format_range", "Issue"]
