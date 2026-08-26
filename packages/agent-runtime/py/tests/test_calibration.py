"""Usage calibration: rolling estimated-vs-observed factors + provider wrapper."""

from __future__ import annotations

import json

import pytest
from steerable_agent_runtime import (
    MODEL_TOKEN_FACTORS,
    CalibratingProvider,
    UsageCalibration,
    estimate_tokens,
    factor_for_model,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage


@pytest.fixture(autouse=True)
def _clean_factors():
    snapshot = dict(MODEL_TOKEN_FACTORS)
    try:
        yield
    finally:
        MODEL_TOKEN_FACTORS.clear()
        MODEL_TOKEN_FACTORS.update(snapshot)


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, usage: LLMUsage | None = None, reply: str = "ok"):
        self.usage = usage or LLMUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110)
        self.reply = reply
        self.requests: list[list[LLMMessage]] = []

    async def complete(self, messages, **kwargs):
        self.requests.append(list(messages))
        return LLMMessage(role="assistant", content=self.reply), self.usage

    async def stream(self, messages, **kwargs):
        self.requests.append(list(messages))
        yield LLMStreamChunk(content_delta=self.reply)
        yield LLMStreamChunk(finish_reason="stop", usage=self.usage)


class TestUsageCalibration:
    def test_factor_is_ratio_of_sums(self) -> None:
        cal = UsageCalibration(min_samples=2, auto_register=False)
        cal.record("m", est_prompt=100, obs_prompt=80)
        cal.record("m", est_prompt=300, obs_prompt=280)
        # (80+280)/(100+300) = 0.9 — ratio of sums, not mean of ratios
        assert cal.factor("m") == pytest.approx(0.9)

    def test_min_samples_gate(self) -> None:
        cal = UsageCalibration(min_samples=5, auto_register=False)
        for _ in range(4):
            cal.record("m", est_prompt=100, obs_prompt=70)
        assert cal.factor("m") is None
        cal.record("m", est_prompt=100, obs_prompt=70)
        assert cal.factor("m") == pytest.approx(0.7)

    def test_skips_degenerate_and_outlier_records(self) -> None:
        cal = UsageCalibration(min_samples=1, auto_register=False)
        cal.record("m", est_prompt=0, obs_prompt=100)  # no estimate
        cal.record("m", est_prompt=100, obs_prompt=0)  # provider sent no usage
        cal.record("m", est_prompt=100, obs_prompt=5000)  # ratio 50 → clipped out
        cal.record("", est_prompt=100, obs_prompt=100)  # no model
        assert cal.models == {}
        cal.record("m", est_prompt=100, obs_prompt=90)
        assert cal.factor("m") == pytest.approx(0.9)

    def test_auto_register_after_threshold(self) -> None:
        cal = UsageCalibration(min_samples=3)
        for _ in range(3):
            cal.record("auto-1", est_prompt=100, obs_prompt=60)
        assert factor_for_model("auto-1") == pytest.approx(0.6)
        # exact model name wins over a family prefix
        assert factor_for_model("auto-1") != factor_for_model("auto")

    def test_persistence_round_trip(self, tmp_path) -> None:
        path = str(tmp_path / "cal.json")
        cal = UsageCalibration(min_samples=2, auto_register=False)
        cal.record("m", est_prompt=200, obs_prompt=150)
        cal.record("m", est_prompt=200, obs_prompt=170, est_completion=50, obs_completion=40)
        cal.save(path)
        loaded = UsageCalibration.load(path, min_samples=2, auto_register=False)
        assert loaded.factor("m") == pytest.approx(320 / 400)
        assert loaded.models["m"].obs_completion == 40

    def test_load_missing_or_corrupt_returns_empty(self, tmp_path) -> None:
        assert UsageCalibration.load(str(tmp_path / "nope.json")).models == {}
        bad = tmp_path / "bad.json"
        bad.write_text("not json{")
        assert UsageCalibration.load(str(bad)).models == {}

    def test_register_factors_on_load(self, tmp_path) -> None:
        path = tmp_path / "cal.json"
        path.write_text(json.dumps({
            "version": 1,
            "models": {"m1": {"requests": 50, "est_prompt": 1000, "obs_prompt": 800,
                              "est_completion": 0, "obs_completion": 0},
                       "m2": {"requests": 3, "est_prompt": 100, "obs_prompt": 50,
                              "est_completion": 0, "obs_completion": 0}},
        }))
        cal = UsageCalibration.load(str(path), min_samples=20)
        registered = cal.register_factors()
        assert registered == {"m1": pytest.approx(0.8)}
        assert factor_for_model("m1") == pytest.approx(0.8)
        assert factor_for_model("m2") == 1.0  # too few samples


class TestCalibratingProvider:
    @pytest.mark.asyncio
    async def test_complete_records_pair(self) -> None:
        inner = FakeProvider(usage=LLMUsage(prompt_tokens=50, completion_tokens=5, total_tokens=55))
        cal = UsageCalibration(min_samples=1, auto_register=False)
        provider = CalibratingProvider(inner, cal)
        messages = [LLMMessage(role="user", content="a" * 40)]
        message, usage = await provider.complete(messages)
        assert message.content == "ok"
        assert usage.prompt_tokens == 50
        entry = cal.models["fake-1"]
        assert entry.est_prompt == estimate_tokens(messages)  # base estimate, no factor
        assert entry.obs_prompt == 50
        assert entry.obs_completion == 5

    @pytest.mark.asyncio
    async def test_stream_passes_chunks_through_and_records(self) -> None:
        inner = FakeProvider(usage=LLMUsage(prompt_tokens=42, completion_tokens=2, total_tokens=44))
        cal = UsageCalibration(min_samples=1, auto_register=False)
        provider = CalibratingProvider(inner, cal)
        chunks = [c async for c in provider.stream([LLMMessage(role="user", content="hi")])]
        assert [c.content_delta for c in chunks if c.content_delta] == ["ok"]
        assert chunks[-1].usage is not None and chunks[-1].usage.prompt_tokens == 42
        assert cal.models["fake-1"].obs_prompt == 42
        assert cal.models["fake-1"].obs_completion == 2

    @pytest.mark.asyncio
    async def test_stream_without_usage_records_nothing(self) -> None:
        class NoUsage(FakeProvider):
            async def stream(self, messages, **kwargs):
                yield LLMStreamChunk(content_delta="x")
                yield LLMStreamChunk(finish_reason="stop")

        cal = UsageCalibration(min_samples=1, auto_register=False)
        provider = CalibratingProvider(NoUsage(), cal)
        async for _ in provider.stream([LLMMessage(role="user", content="hi")]):
            pass
        assert cal.models == {}

    @pytest.mark.asyncio
    async def test_periodic_persist_and_flush(self, tmp_path) -> None:
        path = str(tmp_path / "cal.json")
        # est for "a"*40 is 18; keep the observed/estimated ratio inside the clip band
        inner = FakeProvider(usage=LLMUsage(prompt_tokens=20, completion_tokens=2, total_tokens=22))
        cal = UsageCalibration(min_samples=1, auto_register=False)
        provider = CalibratingProvider(inner, cal, persist_path=path, persist_every=2)
        await provider.complete([LLMMessage(role="user", content="a" * 40)])
        assert not (tmp_path / "cal.json").exists()  # below persist_every
        await provider.complete([LLMMessage(role="user", content="a" * 40)])
        assert (tmp_path / "cal.json").exists()
        loaded = UsageCalibration.load(path, min_samples=1, auto_register=False)
        assert loaded.models["fake-1"].requests == 2

    def test_name_and_model_delegate(self) -> None:
        provider = CalibratingProvider(FakeProvider(), UsageCalibration())
        assert provider.name == "fake"
        assert provider.model == "fake-1"
