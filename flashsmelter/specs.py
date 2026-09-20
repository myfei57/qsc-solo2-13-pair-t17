"""动作参数规格表。

每个动作接受哪些参数、什么类型、是否必填、单位与允许范围，全部在这里声明一次，
作为控制台与 CLI 共用的唯一依据：

- 下发前由 :mod:`flashsmelter.validation` 按规格一次性校验，缺什么、越哪条线、
  哪个参数不认识，一次说全；
- ``actions`` 视图直接读取规格，值班可以先查再发，不用猜参数名和单位。

规格只描述「请求本身」的形状；与炉况相关的门控（状态、闩锁、基线时效）仍在
组件里判断。量程默认值取自 :class:`~flashsmelter.config.Settings`，保证这里给出
的「允许范围」和组件真正把关的范围一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import Settings

NUMBER = "number"
INTEGER = "integer"
TEXT = "text"
BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """单个参数的规格。"""

    name: str
    label: str
    kind: str = TEXT
    required: bool = False
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    # 下限是否开区间：组件门控要求「严格为正」的参数用它，范围会写成 > 0 而不是 >= 0。
    exclusive_minimum: bool = False
    choices: tuple[str, ...] | None = None
    default: Any = None
    max_length: int = 160

    def describe(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.kind,
            "required": self.required,
        }
        if self.unit is not None:
            payload["unit"] = self.unit
        if self.choices is not None:
            payload["choices"] = list(self.choices)
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.exclusive_minimum:
            payload["exclusive_minimum"] = True
        if not self.required and self.default is not None:
            payload["default"] = self.default
        return payload


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个动作的完整参数规格。"""

    action: str
    summary: str
    params: tuple[ParamSpec, ...]
    _by_name: dict[str, ParamSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", {param.name: param for param in self.params})

    def get(self, name: str) -> ParamSpec | None:
        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(param.name for param in self.params)

    def describe(self) -> dict[str, Any]:
        component, _, verb = self.action.partition(".")
        return {
            "action": self.action,
            "component": component,
            "verb": verb,
            "summary": self.summary,
            "endpoint": "/api/" + self.action.replace(".", "/"),
            "params": [param.describe() for param in self.params],
            "required": [param.name for param in self.params if param.required],
        }


# 单位统一用后缀标注，避免再出现「200 还是 200000」的歧义。
UNIT_RATIO = "比例(0~1)"
UNIT_KPA = "kPa"
UNIT_FLOW_NM3H = "Nm³/h"
UNIT_TPH = "t/h"
UNIT_TONS = "t"
UNIT_M = "m"
UNIT_SECONDS = "s"
UNIT_CELSIUS = "℃"


def _common(actor_default: str = "control-room") -> tuple[ParamSpec, ...]:
    """几乎每个动作都接受的通用参数。"""

    return (
        ParamSpec("actor", "操作者", TEXT, default=actor_default, max_length=80),
        ParamSpec("correlation_id", "关联标识", TEXT, required=False, max_length=80),
        ParamSpec("expected_generation", "指令代际", NUMBER, required=False),
    )


def build_action_specs(settings: Settings) -> dict[str, ActionSpec]:
    """按当前配置生成动作规格；量程与组件门控保持同源。"""

    s = settings
    specs: dict[str, ActionSpec] = {}

    def add(action: str, summary: str, params: tuple[ParamSpec, ...]) -> None:
        specs[action] = ActionSpec(action, summary, params)

    add(
        "furnace.start",
        "启动闪速炉（吹扫→点火→富氧建立）",
        (
            *_common(),
            ParamSpec("drum_level", "余热锅炉汽包水位", NUMBER, True, UNIT_RATIO, 0.0, 1.0),
            ParamSpec(
                "fuel_pressure_kpa",
                "燃料压力",
                NUMBER,
                True,
                UNIT_KPA,
                s.burner_fuel_pressure_min_kpa,
                s.burner_fuel_pressure_max_kpa,
            ),
            ParamSpec(
                "air_flow_nm3h", "助燃风流量", NUMBER, True, UNIT_FLOW_NM3H, s.burner_air_flow_min_nm3h
            ),
            ParamSpec(
                "oxygen_baseline",
                "氧分析仪基线浓度",
                NUMBER,
                True,
                UNIT_RATIO,
                s.oxygen_enrichment_min,
                s.oxygen_enrichment_max,
            ),
            ParamSpec("oxygen_baseline_source", "基线来源（分析仪标识）", TEXT, True),
            ParamSpec(
                "oxygen_target",
                "目标富氧浓度",
                NUMBER,
                True,
                UNIT_RATIO,
                s.oxygen_enrichment_min,
                s.oxygen_enrichment_max,
            ),
            ParamSpec("oxygen_flow_nm3h", "富氧流量", NUMBER, True, UNIT_FLOW_NM3H, s.oxygen_flow_min_nm3h),
        ),
    )

    add(
        "furnace.feed",
        "向炉内喷吹精矿",
        (
            *_common(),
            ParamSpec("heat_id", "炉次号", TEXT, True),
            ParamSpec("rate_tph", "喷吹速率", NUMBER, True, UNIT_TPH, 0.0, s.feed_rate_max_tph, True),
            ParamSpec("tons", "本次喷吹吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
        ),
    )

    add(
        "furnace.tap",
        "放渣放铜",
        (
            *_common(),
            ParamSpec("heat_id", "炉次号", TEXT, True),
            ParamSpec("ladle_id", "包子号", TEXT, True),
            ParamSpec("slag_tons", "放渣吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
            ParamSpec("matte_tons", "放铜吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
        ),
    )

    add("furnace.stop", "按顺序停炉", _common())
    add(
        "furnace.latch",
        "闪速炉联锁挂牌",
        (*_common(), ParamSpec("reason", "联锁原因", TEXT, True)),
    )
    add(
        "furnace.reset",
        "闪速炉联锁复位",
        (*_common(), ParamSpec("note", "复位处理说明", TEXT, True)),
    )

    add(
        "burner.ignite",
        "燃烧器点火",
        (
            *_common(),
            ParamSpec(
                "fuel_pressure_kpa",
                "燃料压力",
                NUMBER,
                True,
                UNIT_KPA,
                s.burner_fuel_pressure_min_kpa,
                s.burner_fuel_pressure_max_kpa,
            ),
            ParamSpec(
                "air_flow_nm3h", "助燃风流量", NUMBER, True, UNIT_FLOW_NM3H, s.burner_air_flow_min_nm3h
            ),
        ),
    )
    add(
        "burner.stabilize",
        "燃烧器稳定保持",
        (
            *_common(),
            ParamSpec(
                "hold_seconds",
                "稳定保持时长",
                NUMBER,
                required=False,
                unit=UNIT_SECONDS,
                minimum=s.burner_min_stable_hold_seconds,
            ),
        ),
    )
    add(
        "burner.confirm_flame",
        "确认火焰检测",
        (*_common(), ParamSpec("flame_detected", "火焰是否建立", BOOLEAN, default=True)),
    )
    add("burner.cool_down", "停火冷却", _common())
    add("burner.finish_cooling", "冷却结束", _common())
    add(
        "burner.trip",
        "燃烧器跳闸挂牌",
        (*_common(), ParamSpec("reason", "跳闸原因", TEXT, True)),
    )
    add(
        "burner.attest",
        "刷新燃烧器稳定落盘凭证",
        (
            *_common("control-system"),
            ParamSpec("fuel_pressure_kpa", "燃料压力", NUMBER, False, UNIT_KPA, 0.0),
            ParamSpec("air_flow_nm3h", "助燃风流量", NUMBER, False, UNIT_FLOW_NM3H, 0.0),
        ),
    )
    add(
        "burner.reset",
        "燃烧器故障复位（燃料与风必须回零）",
        (
            *_common(),
            ParamSpec("note", "复位处理说明", TEXT, True),
            ParamSpec("fuel_pressure_kpa", "燃料压力", NUMBER, False, UNIT_KPA, 0.0, 0.0),
            ParamSpec("air_flow_nm3h", "助燃风流量", NUMBER, False, UNIT_FLOW_NM3H, 0.0, 0.0),
        ),
    )

    add(
        "oxygen.set_baseline",
        "标定氧分析仪基线",
        (
            *_common(),
            ParamSpec(
                "value",
                "基线氧浓度",
                NUMBER,
                True,
                UNIT_RATIO,
                s.oxygen_enrichment_min,
                s.oxygen_enrichment_max,
            ),
            ParamSpec("source", "基线来源（分析仪标识）", TEXT, True),
            ParamSpec("observed_at", "观测时间（ISO-8601）", TEXT, required=False),
        ),
    )
    add(
        "oxygen.record_reading",
        "记录氧分析仪读数",
        (
            *_common("analyzer"),
            ParamSpec(
                "value",
                "氧浓度读数",
                NUMBER,
                True,
                UNIT_RATIO,
                s.oxygen_enrichment_min,
                s.oxygen_enrichment_max,
            ),
            ParamSpec("observed_at", "观测时间（ISO-8601）", TEXT, required=False),
        ),
    )
    add(
        "oxygen.establish",
        "建立富氧",
        (
            *_common(),
            ParamSpec(
                "target_enrichment",
                "目标富氧浓度",
                NUMBER,
                True,
                UNIT_RATIO,
                s.oxygen_enrichment_min,
                s.oxygen_enrichment_max,
            ),
            ParamSpec("flow_nm3h", "氧气流量", NUMBER, True, UNIT_FLOW_NM3H, s.oxygen_flow_min_nm3h),
        ),
    )
    add(
        "oxygen.ramp",
        "富氧调档（单步爬坡）",
        (
            *_common(),
            ParamSpec(
                "target_enrichment",
                "目标富氧浓度",
                NUMBER,
                True,
                UNIT_RATIO,
                s.oxygen_enrichment_min,
                s.oxygen_enrichment_max,
            ),
            ParamSpec("step", "单次爬坡步长", NUMBER, False, UNIT_RATIO, 0.0, None, True),
        ),
    )
    add(
        "oxygen.rollback",
        "回滚到上一可靠富氧设定",
        (*_common(), ParamSpec("reason", "回滚原因", TEXT, True)),
    )
    add("oxygen.ramp_down", "停机富氧降档", _common())
    add("oxygen.reset", "富氧系统复位", _common())

    add(
        "conc.arm",
        "精矿喷吹绑定炉次并解锁",
        (*_common(), ParamSpec("heat_id", "炉次号", TEXT, True)),
    )
    add(
        "conc.inject",
        "精矿喷吹",
        (
            *_common(),
            ParamSpec("rate_tph", "喷吹速率", NUMBER, True, UNIT_TPH, 0.0, s.feed_rate_max_tph, True),
            ParamSpec("tons", "本次喷吹吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
        ),
    )
    add("conc.pause", "暂停精矿喷吹", _common())
    add("conc.stop", "停止精矿喷吹", _common())
    add("conc.release_heat", "释放当前炉次绑定", _common())

    add(
        "settler.update",
        "上报沉淀池三层液位",
        (
            *_common(),
            ParamSpec("bath_level_m", "沉淀池总液位", NUMBER, True, UNIT_M, 0.0),
            ParamSpec("slag_thickness_m", "渣层厚度", NUMBER, True, UNIT_M, 0.0),
            ParamSpec("matte_level_m", "冰铜层厚度", NUMBER, True, UNIT_M, 0.0),
        ),
    )
    add(
        "settler.settle",
        "沉淀分层判定",
        (*_common(), ParamSpec("heat_id", "炉次号", TEXT, True)),
    )
    add(
        "settler.begin_tap",
        "开始放料",
        (*_common(), ParamSpec("kind", "放料类型", TEXT, True, choices=("slag", "matte"))),
    )
    add(
        "settler.end_tap",
        "结束放料",
        (
            *_common(),
            ParamSpec("kind", "放料类型", TEXT, True, choices=("slag", "matte")),
            ParamSpec("tons", "本次放料吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
        ),
    )

    add(
        "slag.tap",
        "放渣",
        (
            *_common(),
            ParamSpec("heat_id", "炉次号", TEXT, True),
            ParamSpec("target_tons", "目标放渣吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
        ),
    )
    add("slag.reset", "放渣系统复位", _common())
    add(
        "matte.tap",
        "放铜",
        (
            *_common(),
            ParamSpec("heat_id", "炉次号", TEXT, True),
            ParamSpec("ladle_id", "包子号", TEXT, True),
            ParamSpec("target_tons", "目标放铜吨位", NUMBER, True, UNIT_TONS, 0.0, None, True),
        ),
    )
    add("matte.reset", "放铜系统复位", _common())

    add(
        "conv.charge",
        "转炉入炉",
        (*_common(), ParamSpec("ladle_id", "包子号", TEXT, True)),
    )
    add(
        "conv.blow",
        "转炉吹炼",
        (*_common(), ParamSpec("seconds", "吹炼时长", NUMBER, True, UNIT_SECONDS, 0.0, None, True)),
    )
    add(
        "conv.skim",
        "转炉扒渣",
        (*_common(), ParamSpec("tons", "扒渣吨位", NUMBER, True, UNIT_TONS, 0.0, None, True)),
    )
    add(
        "conv.discharge",
        "转炉倒炉出料",
        (*_common(), ParamSpec("tons", "出料吨位", NUMBER, True, UNIT_TONS, 0.0, None, True)),
    )
    add("conv.finish_batch", "结束转炉批次", _common())

    add(
        "waste.start",
        "余热锅炉投运",
        (
            *_common(),
            ParamSpec(
                "drum_level",
                "汽包水位",
                NUMBER,
                True,
                UNIT_RATIO,
                s.waste_drum_level_min,
                1.0,
            ),
        ),
    )
    add(
        "waste.update",
        "上报余热锅炉运行数据",
        (
            *_common(),
            ParamSpec("drum_level", "汽包水位", NUMBER, True, UNIT_RATIO, 0.0, 1.0),
            ParamSpec("exhaust_temp_c", "烟气温度", NUMBER, True, UNIT_CELSIUS, 0.0),
            ParamSpec("tube_leak", "是否管束泄漏", BOOLEAN, required=False, default=False),
            ParamSpec(
                "steam_flow_tph", "蒸汽流量", NUMBER, required=False, default=0.0, unit=UNIT_TPH, minimum=0.0
            ),
        ),
    )
    add("waste.cooldown", "余热锅炉冷却", _common())
    add("waste.finish_cooling", "余热锅炉冷却结束", _common())
    add(
        "waste.reset",
        "余热锅炉复位",
        (
            *_common(),
            ParamSpec("note", "复位处理说明", TEXT, True),
            ParamSpec("drum_level", "汽包水位", NUMBER, True, UNIT_RATIO, s.waste_drum_level_min, 1.0),
            ParamSpec(
                "exhaust_temp_c", "烟气温度", NUMBER, True, UNIT_CELSIUS, 0.0, s.waste_exhaust_temp_max_c
            ),
            ParamSpec("tube_leak", "是否管束泄漏", BOOLEAN, required=False, default=False),
        ),
    )

    return specs


__all__ = [
    "ParamSpec",
    "ActionSpec",
    "build_action_specs",
    "NUMBER",
    "INTEGER",
    "TEXT",
    "BOOLEAN",
]
