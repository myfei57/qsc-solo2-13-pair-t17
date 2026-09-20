"""组装根。

控制台与 CLI 都从这里拿到同一套组件与动作注册表：状态只装配一次，动作只在
一处定义，避免出现「控制台能调、CLI 调不了」这类不一致。

每个动作都带一份 :class:`ParamSpec` 参数清单：必填缺失、取值越界、参数名不
认识，都在动作执行之前一次性拦截并指名道姓地报出来，值班人员不用打电话猜。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .audit import AuditLog
from .burner import Burner
from .component import Component, ensure_actor
from .conc import ConcentrateSystem
from .config import Settings
from .conv import Converter
from .errors import ValidationError
from .furnace import FlashFurnace
from .matte import MatteTap
from .ns import Namespace
from .oxygen import OxygenSystem
from .params import Field, ParamSpec, Params
from .runtime import Clock, Generation, Metrics, RuntimeContext
from .settler import Settler
from .slag import SlagTap
from .store import DurableStore
from .waste import WasteHeatBoiler

ActionHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]

# 几乎每个动作都有的三个公共参数，只声明一次。
_ACTOR = Field("actor", "text", required=False, default="control-room", label="操作者")
_CORRELATION = Field("correlation_id", "text", required=False, label="关联单号")
_GENERATION = Field("expected_generation", "integer", required=False, label="期望指令代际")


@dataclass(frozen=True, slots=True)
class _Action:
    """一个已注册动作：参数清单 + 执行体。"""

    spec: ParamSpec
    handler: ActionHandler


class Application:
    """把配置、持久化、组件与控制台动作装配成一个可运行实例。"""

    def __init__(self, settings: Settings, *, clock: Clock | None = None) -> None:
        settings.validate()
        settings.ensure_directories()
        self.settings = settings
        self.clock = clock or Clock()
        self.namespace = Namespace.parse(settings.namespace)
        self.metrics = Metrics(self.clock)
        self.generation = Generation(self.clock)
        self.store = DurableStore(settings.root, clock=self.clock)
        self.audit = AuditLog(self.store, self.namespace, self.clock)
        self.ctx = RuntimeContext(
            settings=settings,
            namespace=self.namespace,
            store=self.store,
            clock=self.clock,
            metrics=self.metrics,
            generation=self.generation,
            audit=self.audit,
        )
        self._build_components()
        self._actions: dict[str, _Action] = self._build_actions()

    # ------------------------------------------------------------- 组件装配
    def _build_components(self) -> None:
        ctx = self.ctx
        self.burner = Burner(ctx)
        self.waste = WasteHeatBoiler(ctx)
        self.settler = Settler(ctx)
        self.oxygen = OxygenSystem(ctx)
        self.slag = SlagTap(ctx, settler=self.settler, waste=self.waste)
        self.matte = MatteTap(ctx, settler=self.settler, waste=self.waste, slag=self.slag)
        self.conv = Converter(ctx, matte=self.matte)
        self.conc = ConcentrateSystem(
            ctx, burner=self.burner, oxygen=self.oxygen, waste=self.waste, settler=self.settler
        )
        self.furnace = FlashFurnace(
            ctx,
            burner=self.burner,
            oxygen=self.oxygen,
            conc=self.conc,
            settler=self.settler,
            slag=self.slag,
            matte=self.matte,
            waste=self.waste,
            converter=self.conv,
        )
        self.slag.bind_matte(self.matte)
        self.matte.bind_converter(self.conv)
        self.oxygen.bind_feed_port(self.conc)
        self.components: tuple[Component, ...] = (
            self.furnace,
            self.burner,
            self.oxygen,
            self.conc,
            self.settler,
            self.slag,
            self.matte,
            self.conv,
            self.waste,
        )
        self._by_name: dict[str, Component] = {component.name: component for component in self.components}

    # ------------------------------------------------------------- 动作注册
    def _build_actions(self) -> dict[str, _Action]:
        actions: dict[str, _Action] = {}

        def register(name: str, fields: list[Field]) -> Callable[[ActionHandler], ActionHandler]:
            spec = ParamSpec(name, fields)

            def decorate(handler: ActionHandler) -> ActionHandler:
                if name in actions:
                    raise ValidationError("动作重复注册", details={"action": name})
                actions[name] = _Action(spec=spec, handler=handler)
                return handler

            return decorate

        @register("furnace.start", [
            _ACTOR,
            Field("drum_level", "number", minimum=0.0, maximum=1.0, unit="比例", label="汽包液位"),
            Field("fuel_pressure_kpa", "number", minimum=0.0, unit="kPa", label="燃料压力"),
            Field("air_flow_nm3h", "number", minimum=0.0, unit="Nm³/h", label="助燃风量"),
            Field("oxygen_baseline", "number", minimum=0.0, maximum=1.0, unit="比例", label="氧浓基线"),
            Field("oxygen_baseline_source", "text", label="氧浓基线来源"),
            Field("oxygen_target", "number", minimum=0.0, maximum=1.0, unit="比例", label="目标氧浓"),
            Field("oxygen_flow_nm3h", "number", minimum=0.0, unit="Nm³/h", label="氧气流量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _furnace_start(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.furnace.start(**values)

        @register("furnace.feed", [
            _ACTOR,
            Field("heat_id", "text", label="炉次号"),
            Field("rate_tph", "number", minimum=0.0, unit="t/h", label="给料速率"),
            Field("tons", "number", minimum=0.0, unit="t", label="喷吹总量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _furnace_feed(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.furnace.feed(**values)

        @register("furnace.tap", [
            _ACTOR,
            Field("heat_id", "text", label="炉次号"),
            Field("ladle_id", "text", label="铜包号"),
            Field("slag_tons", "number", minimum=0.0, unit="t", label="放渣量"),
            Field("matte_tons", "number", minimum=0.0, unit="t", label="放铜量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _furnace_tap(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.furnace.tap(**values)

        @register("furnace.stop", [_ACTOR, _CORRELATION, _GENERATION])
        def _furnace_stop(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.furnace.stop(**values)

        @register("furnace.latch", [
            _ACTOR,
            Field("reason", "text", label="联锁原因"),
            _CORRELATION,
        ])
        def _furnace_latch(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.furnace.latch(**values)

        @register("furnace.reset", [
            _ACTOR,
            Field("note", "text", label="处理说明"),
            _CORRELATION,
            _GENERATION,
        ])
        def _furnace_reset(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.furnace.reset(**values)

        @register("burner.ignite", [
            _ACTOR,
            Field("fuel_pressure_kpa", "number", minimum=0.0, unit="kPa", label="燃料压力"),
            Field("air_flow_nm3h", "number", minimum=0.0, unit="Nm³/h", label="助燃风量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _burner_ignite(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.ignite(**values)

        @register("burner.stabilize", [
            _ACTOR,
            Field("hold_seconds", "number", required=False, minimum=0.0, unit="s", label="稳燃保持时长"),
            _CORRELATION,
            _GENERATION,
        ])
        def _burner_stabilize(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.stabilize(**values)

        @register("burner.confirm_flame", [
            _ACTOR,
            Field("flame_detected", "boolean", required=False, default=True, label="火焰检测信号"),
            _CORRELATION,
            _GENERATION,
        ])
        def _burner_confirm_flame(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.confirm_flame(**values)

        @register("burner.cool_down", [_ACTOR, _CORRELATION, _GENERATION])
        def _burner_cool_down(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.cool_down(**values)

        @register("burner.finish_cooling", [_ACTOR, _CORRELATION, _GENERATION])
        def _burner_finish_cooling(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.finish_cooling(**values)

        @register("burner.trip", [
            _ACTOR,
            Field("reason", "text", label="跳闸原因"),
            _CORRELATION,
        ])
        def _burner_trip(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.trip(**values)

        @register("burner.attest", [
            Field("actor", "text", required=False, default="control-system", label="操作者"),
            Field("fuel_pressure_kpa", "number", required=False, minimum=0.0, unit="kPa", label="燃料压力"),
            Field("air_flow_nm3h", "number", required=False, minimum=0.0, unit="Nm³/h", label="助燃风量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _burner_attest(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.attest(**values)

        @register("burner.reset", [
            _ACTOR,
            Field("note", "text", label="处理说明"),
            Field("fuel_pressure_kpa", "number", required=False, default=0.0, minimum=0.0, unit="kPa", label="燃料压力"),
            Field("air_flow_nm3h", "number", required=False, default=0.0, minimum=0.0, unit="Nm³/h", label="助燃风量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _burner_reset(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.burner.reset(**values)

        @register("oxygen.set_baseline", [
            _ACTOR,
            Field("value", "number", minimum=0.0, maximum=1.0, unit="比例", label="氧浓基线值"),
            Field("source", "text", label="数据来源"),
            Field("observed_at", "text", required=False, label="观测时间（ISO-8601）"),
            _CORRELATION,
            _GENERATION,
        ])
        def _oxygen_baseline(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.set_baseline(**values)

        @register("oxygen.record_reading", [
            Field("actor", "text", required=False, default="analyzer", label="操作者"),
            Field("value", "number", minimum=0.0, maximum=1.0, unit="比例", label="氧浓读数"),
            Field("observed_at", "text", required=False, label="观测时间（ISO-8601）"),
            _CORRELATION,
        ])
        def _oxygen_reading(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.record_reading(**values)

        @register("oxygen.establish", [
            _ACTOR,
            Field("target_enrichment", "number", minimum=0.0, maximum=1.0, unit="比例", label="目标富氧浓度"),
            Field("flow_nm3h", "number", minimum=0.0, unit="Nm³/h", label="氧气流量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _oxygen_establish(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.establish(**values)

        @register("oxygen.ramp", [
            _ACTOR,
            Field("target_enrichment", "number", minimum=0.0, maximum=1.0, unit="比例", label="目标富氧浓度"),
            Field("step", "number", required=False, minimum=0.0, unit="比例", label="单次爬坡步长"),
            _CORRELATION,
            _GENERATION,
        ])
        def _oxygen_ramp(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.ramp(**values)

        @register("oxygen.rollback", [
            _ACTOR,
            Field("reason", "text", label="回退原因"),
            _CORRELATION,
            _GENERATION,
        ])
        def _oxygen_rollback(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.rollback(**values)

        @register("oxygen.ramp_down", [_ACTOR, _CORRELATION, _GENERATION])
        def _oxygen_ramp_down(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.ramp_down(**values)

        @register("oxygen.reset", [_ACTOR, _CORRELATION, _GENERATION])
        def _oxygen_reset(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.oxygen.reset(**values)

        @register("conc.arm", [
            _ACTOR,
            Field("heat_id", "text", label="炉次号"),
            _CORRELATION,
            _GENERATION,
        ])
        def _conc_arm(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conc.arm(**values)

        @register("conc.inject", [
            _ACTOR,
            Field("rate_tph", "number", minimum=0.0, unit="t/h", label="给料速率"),
            Field("tons", "number", minimum=0.0, unit="t", label="喷吹总量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _conc_inject(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conc.inject(**values)

        @register("conc.pause", [_ACTOR, _CORRELATION, _GENERATION])
        def _conc_pause(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conc.pause(**values)

        @register("conc.stop", [_ACTOR, _CORRELATION, _GENERATION])
        def _conc_stop(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conc.stop(**values)

        @register("conc.release_heat", [_ACTOR, _CORRELATION, _GENERATION])
        def _conc_release(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conc.release_heat(**values)

        @register("settler.update", [
            _ACTOR,
            Field("bath_level_m", "number", minimum=0.0, unit="m", label="熔池液位"),
            Field("slag_thickness_m", "number", minimum=0.0, unit="m", label="渣层厚度"),
            Field("matte_level_m", "number", minimum=0.0, unit="m", label="冰铜层厚度"),
            _CORRELATION,
            _GENERATION,
        ])
        def _settler_update(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.settler.update(**values)

        @register("settler.settle", [
            _ACTOR,
            Field("heat_id", "text", label="炉次号"),
            _CORRELATION,
            _GENERATION,
        ])
        def _settler_settle(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.settler.settle(**values)

        @register("settler.begin_tap", [
            _ACTOR,
            Field("kind", "text", label="排放类型（slag 渣 / matte 冰铜）"),
            _CORRELATION,
            _GENERATION,
        ])
        def _settler_begin_tap(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.settler.begin_tap(**values)

        @register("settler.end_tap", [
            _ACTOR,
            Field("kind", "text", label="排放类型（slag 渣 / matte 冰铜）"),
            Field("tons", "number", minimum=0.0, unit="t", label="排放量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _settler_end_tap(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.settler.end_tap(**values)

        @register("slag.tap", [
            _ACTOR,
            Field("heat_id", "text", label="炉次号"),
            Field("target_tons", "number", minimum=0.0, unit="t", label="目标放渣量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _slag_tap(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.slag.tap(**values)

        @register("slag.reset", [_ACTOR, _CORRELATION, _GENERATION])
        def _slag_reset(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.slag.reset(**values)

        @register("matte.tap", [
            _ACTOR,
            Field("heat_id", "text", label="炉次号"),
            Field("ladle_id", "text", label="铜包号"),
            Field("target_tons", "number", minimum=0.0, unit="t", label="目标放铜量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _matte_tap(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.matte.tap(**values)

        @register("matte.reset", [_ACTOR, _CORRELATION, _GENERATION])
        def _matte_reset(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.matte.reset(**values)

        @register("conv.charge", [
            _ACTOR,
            Field("ladle_id", "text", label="铜包号"),
            _CORRELATION,
            _GENERATION,
        ])
        def _conv_charge(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conv.charge(**values)

        @register("conv.blow", [
            _ACTOR,
            Field("seconds", "number", minimum=0.0, unit="s", label="吹炼时长"),
            _CORRELATION,
            _GENERATION,
        ])
        def _conv_blow(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conv.blow(**values)

        @register("conv.skim", [
            _ACTOR,
            Field("tons", "number", minimum=0.0, unit="t", label="扒渣量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _conv_skim(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conv.skim(**values)

        @register("conv.discharge", [
            _ACTOR,
            Field("tons", "number", minimum=0.0, unit="t", label="放出量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _conv_discharge(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conv.discharge(**values)

        @register("conv.finish_batch", [_ACTOR, _CORRELATION, _GENERATION])
        def _conv_finish(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.conv.finish_batch(**values)

        @register("waste.start", [
            _ACTOR,
            Field("drum_level", "number", minimum=0.0, maximum=1.0, unit="比例", label="汽包液位"),
            _CORRELATION,
            _GENERATION,
        ])
        def _waste_start(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.waste.start(**values)

        @register("waste.update", [
            _ACTOR,
            Field("drum_level", "number", minimum=0.0, maximum=1.0, unit="比例", label="汽包液位"),
            Field("exhaust_temp_c", "number", minimum=0.0, unit="°C", label="排烟温度"),
            Field("tube_leak", "boolean", required=False, default=False, label="炉管泄漏标志"),
            Field("steam_flow_tph", "number", required=False, default=0.0, minimum=0.0, unit="t/h", label="蒸汽流量"),
            _CORRELATION,
            _GENERATION,
        ])
        def _waste_update(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.waste.update(**values)

        @register("waste.cooldown", [_ACTOR, _CORRELATION, _GENERATION])
        def _waste_cooldown(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.waste.cooldown(**values)

        @register("waste.finish_cooling", [_ACTOR, _CORRELATION, _GENERATION])
        def _waste_finish_cooling(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.waste.finish_cooling(**values)

        @register("waste.reset", [
            _ACTOR,
            Field("note", "text", label="处理说明"),
            Field("drum_level", "number", minimum=0.0, maximum=1.0, unit="比例", label="汽包液位"),
            Field("exhaust_temp_c", "number", minimum=0.0, unit="°C", label="排烟温度"),
            Field("tube_leak", "boolean", required=False, default=False, label="炉管泄漏标志"),
            _CORRELATION,
            _GENERATION,
        ])
        def _waste_reset(values: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.waste.reset(**values)

        return actions

    # ------------------------------------------------------------- 对外接口
    @property
    def actions(self) -> Mapping[str, ActionHandler]:
        return {name: entry.handler for name, entry in self._actions.items()}

    def component(self, name: str) -> Component:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValidationError("未知组件", details={"component": name}) from exc

    def invoke(self, action: str, params: Mapping[str, Any] | None = None, *, source: str = "api") -> Mapping[str, Any]:
        try:
            entry = self._actions[action]
        except KeyError as exc:
            raise ValidationError(
                f"未知动作「{action}」，可用动作见 /api/actions",
                details={"action": action, "known": sorted(self._actions)},
            ) from exc
        parsed = params if isinstance(params, Params) else Params(params, source=source)
        values = entry.spec.extract(parsed)
        return entry.handler(values)

    def state(self) -> Mapping[str, Any]:
        service = dict(self.ctx.describe())
        service["version"] = _version()
        return {
            "service": service,
            "heat": self.furnace.heat_report(),
            "components": {component.name: dict(component.snapshot()) for component in self.components},
            "metrics": self.metrics.snapshot(),
        }

    def audit_events(
        self,
        *,
        limit: int = 50,
        since_seq: int = 0,
        action: str | None = None,
        target: str | None = None,
        outcome: str | None = None,
        actor: str | None = None,
    ) -> list[Mapping[str, Any]]:
        events = self.audit.query(
            limit=limit,
            since_seq=since_seq,
            action=action,
            target=target,
            outcome=outcome,
            actor=actor,
        )
        return [event.to_dict() for event in events]

    def verify(self) -> Mapping[str, Any]:
        report = self.store.verify()
        payload = dict(report.to_dict())
        payload["components"] = {component.name: component.status()["state"] for component in self.components}
        payload["audit_length"] = self.audit.length()
        return payload

    def describe_actions(self) -> list[Mapping[str, Any]]:
        return [
            {
                "action": name,
                "component": name.split(".", 1)[0],
                "verb": name.split(".", 1)[1],
                "endpoint": "/api/" + name.replace(".", "/"),
                "params": entry.spec.describe(),
            }
            for name, entry in sorted(self._actions.items())
        ]


def _version() -> str:
    from . import __version__

    return __version__


def build_application(settings: Settings, *, clock: Clock | None = None) -> Application:
    return Application(settings, clock=clock)


__all__ = ["Application", "build_application", "ActionHandler"]
