"""控制台与 CLI 共用的参数解析与动作参数规范。

两层职责：

* :class:`Params` 把原始请求参数收敛成有类型的取值。报错时指名道姓——哪个
  参数、收到的是什么、允许范围是多少，都写在消息里，值班人员不用猜。
* :class:`Field` / :class:`ParamSpec` 把一个动作接受的参数完整声明出来。
  动作执行之前先整体校验：缺哪个必填、哪个取值越界（附允许范围）、哪个参数
  名根本不认识，全部问题一次性列清，而不是让人改一个试一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import ConfigurationError, ValidationError

_TRUE_LITERALS = {"true", "1", "yes", "on", "y"}
_FALSE_LITERALS = {"false", "0", "no", "off", "n"}
_BOOLEAN_HINT = "可填 true/false、1/0、yes/no、on/off"

_KIND_NAMES = {"text": "文本", "number": "数值", "integer": "整数", "boolean": "布尔值"}


def _range_hint(minimum: float | None, maximum: float | None) -> str:
    if minimum is not None and maximum is not None:
        return f"允许范围 {minimum} ~ {maximum}"
    if minimum is not None:
        return f"下限 {minimum}"
    if maximum is not None:
        return f"上限 {maximum}"
    return ""


class Params:
    """把原始请求参数收敛成有类型的取值。"""

    def __init__(self, raw: Mapping[str, Any] | None = None, *, source: str = "request") -> None:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValidationError("参数必须是映射", details={"source": source, "type": type(raw).__name__})
        self._raw = {str(key): value for key, value in raw.items()}
        self._source = source
        self._consumed: set[str] = set()

    @property
    def raw(self) -> Mapping[str, Any]:
        return dict(self._raw)

    @property
    def source(self) -> str:
        return self._source

    def keys(self) -> Iterable[str]:
        return tuple(self._raw.keys())

    def has(self, name: str) -> bool:
        return name in self._raw and self._raw[name] not in (None, "")

    def text(
        self,
        name: str,
        *,
        required: bool = True,
        default: str | None = None,
        max_length: int = 160,
    ) -> str:
        self._consumed.add(name)
        value = self._raw.get(name, default)
        if value is None or value == "":
            if required:
                raise ValidationError(
                    f"缺少必填参数「{name}」（文本）",
                    details={"source": self._source, "param": name, "problem": "missing"},
                )
            return "" if default is None else default
        text = str(value).strip()
        if not text and required:
            raise ValidationError(
                f"参数「{name}」不能为空字符串",
                details={"source": self._source, "param": name, "problem": "empty"},
            )
        if len(text) > max_length:
            raise ValidationError(
                f"参数「{name}」长度 {len(text)} 超过上限 {max_length} 字符",
                details={
                    "source": self._source,
                    "param": name,
                    "problem": "too-long",
                    "length": len(text),
                    "max": max_length,
                },
            )
        return text

    def number(
        self,
        name: str,
        *,
        required: bool = True,
        default: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        self._consumed.add(name)
        value = self._raw.get(name, default)
        if value is None or value == "":
            if required:
                hint = _range_hint(minimum, maximum)
                suffix = f"，{hint}" if hint else ""
                raise ValidationError(
                    f"缺少必填参数「{name}」（数值{suffix}）",
                    details={
                        "source": self._source,
                        "param": name,
                        "problem": "missing",
                        "minimum": minimum,
                        "maximum": maximum,
                    },
                )
            if default is None:
                raise ValidationError(
                    f"参数「{name}」既非必填也无默认值",
                    details={"source": self._source, "param": name, "problem": "missing"},
                )
            value = default
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"参数「{name}」的取值 {value!r} 不是数值",
                details={"source": self._source, "param": name, "problem": "not-a-number", "value": value},
            ) from exc
        if number != number:  # NaN
            raise ValidationError(
                f"参数「{name}」不能是 NaN",
                details={"source": self._source, "param": name, "problem": "nan"},
            )
        if minimum is not None and number < minimum:
            raise ValidationError(
                f"参数「{name}」取值 {number} 低于下限 {minimum}（{_range_hint(minimum, maximum)}）",
                details={
                    "source": self._source,
                    "param": name,
                    "problem": "below-minimum",
                    "value": number,
                    "minimum": minimum,
                    "maximum": maximum,
                },
            )
        if maximum is not None and number > maximum:
            raise ValidationError(
                f"参数「{name}」取值 {number} 超过上限 {maximum}（{_range_hint(minimum, maximum)}）",
                details={
                    "source": self._source,
                    "param": name,
                    "problem": "above-maximum",
                    "value": number,
                    "minimum": minimum,
                    "maximum": maximum,
                },
            )
        return number

    def integer(
        self,
        name: str,
        *,
        required: bool = True,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        raw = self.number(
            name,
            required=required,
            default=None if default is None else float(default),
            minimum=None if minimum is None else float(minimum),
            maximum=None if maximum is None else float(maximum),
        )
        if abs(raw - round(raw)) > 1e-9:
            raise ValidationError(
                f"参数「{name}」取值 {raw} 不是整数",
                details={"source": self._source, "param": name, "problem": "not-integer", "value": raw},
            )
        return int(round(raw))

    def boolean(self, name: str, *, required: bool = True, default: bool | None = None) -> bool:
        self._consumed.add(name)
        value = self._raw.get(name, default)
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            if required:
                raise ValidationError(
                    f"缺少必填参数「{name}」（布尔值，{_BOOLEAN_HINT}）",
                    details={"source": self._source, "param": name, "problem": "missing"},
                )
            assert default is not None
            return default
        text = str(value).strip().lower()
        if text in _TRUE_LITERALS:
            return True
        if text in _FALSE_LITERALS:
            return False
        raise ValidationError(
            f"参数「{name}」的取值 {value!r} 不是布尔值（{_BOOLEAN_HINT}）",
            details={"source": self._source, "param": name, "problem": "not-a-boolean", "value": value},
        )

    def optional_number(self, name: str, **kwargs: Any) -> float | None:
        if not self.has(name):
            self._consumed.add(name)
            return None
        return self.number(name, **kwargs)

    def optional_text(self, name: str, **kwargs: Any) -> str | None:
        if not self.has(name):
            self._consumed.add(name)
            return None
        return self.text(name, **kwargs)

    def mapping(self, name: str, *, required: bool = False) -> dict[str, Any]:
        self._consumed.add(name)
        value = self._raw.get(name)
        if value is None:
            if required:
                raise ValidationError(
                    f"缺少必填参数「{name}」（对象）",
                    details={"source": self._source, "param": name, "problem": "missing"},
                )
            return {}
        if not isinstance(value, Mapping):
            raise ValidationError(
                f"参数「{name}」必须是对象，收到的是 {type(value).__name__}",
                details={"source": self._source, "param": name, "problem": "not-a-mapping", "type": type(value).__name__},
            )
        return {str(key): item for key, item in value.items()}

    def reject_unknown(self, allowed: Iterable[str]) -> None:
        permitted = set(allowed)
        unknown = sorted(set(self._raw) - permitted)
        if unknown:
            raise ValidationError(
                f"不认识的参数「{'、'.join(unknown)}」，该接口只接受：{'、'.join(sorted(permitted))}",
                details={
                    "source": self._source,
                    "problem": "unknown",
                    "unknown": unknown,
                    "allowed": sorted(permitted),
                },
            )

    def as_dict(self) -> dict[str, Any]:
        return dict(self._raw)


@dataclass(frozen=True, slots=True)
class Field:
    """动作参数声明：名字、类型、必填性、允许范围、单位与中文名。"""

    name: str
    kind: str
    required: bool = True
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    label: str = ""
    max_length: int = 160

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("参数名不能为空")
        if self.kind not in _KIND_NAMES:
            raise ConfigurationError(
                "参数类型不支持",
                details={"param": self.name, "kind": self.kind, "known": sorted(_KIND_NAMES)},
            )

    def tag(self) -> str:
        """「drum_level」（汽包液位）这样的指名片段。"""

        return f"「{self.name}」（{self.label}）" if self.label else f"「{self.name}」"

    def expectation(self, *, include_kind: bool = True) -> str:
        """「数值，单位 t/h，允许范围 0.0 ~ 1.0」这样的期望描述。"""

        parts = [_KIND_NAMES[self.kind]] if include_kind else []
        if self.kind == "boolean":
            parts.append(_BOOLEAN_HINT)
        if self.unit:
            parts.append(f"单位 {self.unit}")
        hint = _range_hint(self.minimum, self.maximum)
        if hint:
            parts.append(hint)
        return "，".join(parts)

    def describe_missing(self) -> str:
        """缺失时的完整指名：「drum_level」（汽包液位，数值，允许范围 0.0 ~ 1.0）。"""

        parts = [self.label] if self.label else []
        parts.append(self.expectation())
        return f"「{self.name}」（{'，'.join(parts)}）"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "required": self.required,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
            "label": self.label,
        }


class ParamSpec:
    """一个动作的完整参数清单：先整体校验，再放行执行。"""

    def __init__(self, action: str, fields: Iterable[Field]) -> None:
        self._action = action
        self._fields = tuple(fields)
        names = [field.name for field in self._fields]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConfigurationError(
                "动作参数重复声明", details={"action": action, "params": duplicates}
            )
        self._names = frozenset(names)

    @property
    def action(self) -> str:
        return self._action

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self._fields)

    @property
    def fields(self) -> tuple[Field, ...]:
        return self._fields

    def describe(self) -> list[dict[str, Any]]:
        return [field.describe() for field in self._fields]

    def extract(self, params: Params) -> dict[str, Any]:
        """校验并取出全部取值；有任何问题就聚合抛出，绝不放行执行。"""

        values: dict[str, Any] = {}
        issues: list[dict[str, Any]] = []
        for field in self._fields:
            if not params.has(field.name):
                if field.required:
                    issues.append(
                        {
                            "param": field.name,
                            "problem": "missing",
                            "message": f"缺少必填参数{field.describe_missing()}",
                        }
                    )
                else:
                    values[field.name] = field.default
                continue
            try:
                values[field.name] = self._parse_field(params, field)
            except ValidationError as exc:
                issues.append(self._value_issue(field, exc))
        for name in sorted(set(params.raw) - self._names):
            issues.append(
                {
                    "param": name,
                    "problem": "unknown",
                    "message": (
                        f"不认识的参数「{name}」，动作 {self._action} "
                        f"只接受：{'、'.join(self.names)}"
                    ),
                }
            )
        if issues:
            _raise_issues(self._action, params.source, issues)
        return values

    # -------------------------------------------------------------- 内部
    def _parse_field(self, params: Params, field: Field) -> Any:
        if field.kind == "text":
            return params.text(field.name, max_length=field.max_length)
        if field.kind == "number":
            return params.number(field.name, minimum=field.minimum, maximum=field.maximum)
        if field.kind == "integer":
            return params.integer(field.name, minimum=field.minimum, maximum=field.maximum)
        if field.kind == "boolean":
            return params.boolean(field.name)
        raise ConfigurationError(  # pragma: no cover - 构造期已拦截
            "参数类型不支持", details={"param": field.name, "kind": field.kind}
        )

    def _value_issue(self, field: Field, exc: ValidationError) -> dict[str, Any]:
        details = dict(exc.details)
        problem = str(details.get("problem", "invalid"))
        issue: dict[str, Any] = {
            "param": field.name,
            "problem": problem,
            "message": _compose_value_message(field, problem, details, exc.message),
        }
        for key in ("value", "minimum", "maximum", "length", "max"):
            if key in details and details[key] is not None:
                issue[key] = details[key]
        return issue


def _compose_value_message(field: Field, problem: str, details: Mapping[str, Any], fallback: str) -> str:
    """把取值类问题写成带中文名、单位与允许范围的一句话。"""

    tag = f"参数{field.tag()}"
    hint = _range_hint(field.minimum, field.maximum)
    unit = f"，单位 {field.unit}" if field.unit else ""
    if problem == "not-a-number":
        rest = field.expectation(include_kind=False)
        suffix = f"（{rest}）" if rest else ""
        return f"{tag}的取值 {details.get('value')!r} 不是{_KIND_NAMES[field.kind]}{suffix}"
    if problem == "nan":
        return f"{tag}不能是 NaN"
    if problem == "below-minimum":
        return f"{tag}取值 {details.get('value')} 低于下限 {field.minimum}（{hint}{unit}）"
    if problem == "above-maximum":
        return f"{tag}取值 {details.get('value')} 超过上限 {field.maximum}（{hint}{unit}）"
    if problem == "not-integer":
        return f"{tag}取值 {details.get('value')} 不是整数"
    if problem == "not-a-boolean":
        return f"{tag}的取值 {details.get('value')!r} 不是布尔值（{_BOOLEAN_HINT}）"
    if problem == "too-long":
        return f"{tag}长度 {details.get('length')} 超过上限 {details.get('max')} 字符"
    if problem == "empty":
        return f"{tag}不能为空字符串"
    return fallback


def _raise_issues(action: str, source: str, issues: list[dict[str, Any]]) -> None:
    details: dict[str, Any] = {"action": action, "source": source, "issues": issues}
    if len(issues) == 1:
        issue = issues[0]
        details["param"] = issue["param"]
        details["problem"] = issue["problem"]
        raise ValidationError(issue["message"], details=details)
    listing = "\n".join(f"{index}. {issue['message']}" for index, issue in enumerate(issues, 1))
    raise ValidationError(f"参数校验未通过，共 {len(issues)} 处问题：\n{listing}", details=details)


__all__ = ["Params", "Field", "ParamSpec"]
