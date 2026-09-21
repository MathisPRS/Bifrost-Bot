"""La porte de verification, et la sequence qui les enchaine.

C'est la piece centrale du projet. Trois regles, valables pour tous les jeux :

  1. `hold` — une condition doit TENIR pendant une duree, pas seulement
     apparaitre une fois. Sans ca, le creux de consommation de quelques
     secondes d'un cycle de redemarrage est indiscernable d'une extinction, et
     on coupe le courant d'une machine qui s'apprete a repartir.

  2. Un resultat INCONNU n'est pas un echec, mais ce n'est surtout pas un
     succes : il remet le compteur de maintien a zero, exactement comme une
     valeur hors critere. Une lecture ratee ne vaut jamais zero watt.

  3. Une porte qui tombe arrete la sequence. Rien n'est force, rien n'est
     coupe, et le rapport dit exactement ou on s'est arrete.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class Verdict(Enum):
    PASS = "passee"
    FAIL = "echouee"
    TIMEOUT = "delai depasse"
    ABORTED = "abandonnee"
    SKIPPED = "ignoree"


@dataclass(frozen=True)
class Check:
    """Resultat d'une verification ponctuelle.

    `ok=None` signifie "je n'ai pas pu savoir" — jamais "non".
    """
    ok: bool | None
    detail: str = ""

    @staticmethod
    def yes(detail: str = "") -> "Check":
        return Check(True, detail)

    @staticmethod
    def no(detail: str = "") -> "Check":
        return Check(False, detail)

    @staticmethod
    def unknown(detail: str = "") -> "Check":
        return Check(None, detail)


@dataclass
class Gate:
    name: str
    check: Callable[[], Awaitable[Check]]
    timeout: float
    interval: float = 2.0
    hold: float = 0.0
    # Une porte facultative qui echoue n'arrete pas la sequence ; elle est
    # simplement rapportee. Reservee a ce qui est informatif.
    optional: bool = False


@dataclass
class GateReport:
    gate: str
    verdict: Verdict
    detail: str = ""
    elapsed_s: float = 0.0
    samples: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.PASS, Verdict.SKIPPED)

    def __str__(self) -> str:
        mark = {"passee": "✅", "echouee": "❌", "delai depasse": "⏱️",
                "abandonnee": "🛑", "ignoree": "⏭️"}[self.verdict.value]
        d = f" — {self.detail}" if self.detail else ""
        return f"{mark} {self.gate}{d}"


async def run_gate(
    gate: Gate,
    cancel: asyncio.Event | None = None,
    on_sample: Callable[[str, float], Awaitable[None]] | None = None,
) -> GateReport:
    """`on_sample` est appele a chaque releve avec (detail, secondes ecoulees).

    C'est ce qui permet a l'affichage de vivre pendant une attente longue :
    sans lui, une porte de 90 s serait un ecran fige.
    """
    t0 = time.monotonic()
    held_since: float | None = None
    samples = 0
    last = ""

    while True:
        if cancel is not None and cancel.is_set():
            return GateReport(gate.name, Verdict.ABORTED, "annulee",
                              time.monotonic() - t0, samples)

        try:
            res = await gate.check()
        except Exception as exc:                        # noqa: BLE001
            res = Check.unknown(f"{type(exc).__name__}: {exc}")
        samples += 1
        last = res.detail
        now = time.monotonic()
        if on_sample is not None:
            tenu = ""
            if gate.hold > 0 and held_since is not None:
                tenu = f" · tenu {now - held_since:.0f}/{gate.hold:.0f} s"
            await on_sample((res.detail or "en attente") + tenu, now - t0)

        if res.ok is True:
            if gate.hold <= 0:
                return GateReport(gate.name, Verdict.PASS, res.detail, now - t0, samples)
            if held_since is None:
                held_since = now
            elif now - held_since >= gate.hold:
                held = f"{res.detail} (tenu {gate.hold:.0f} s)" if res.detail else f"tenu {gate.hold:.0f} s"
                return GateReport(gate.name, Verdict.PASS, held, now - t0, samples)
        else:
            # FAUX comme INCONNU remettent le maintien a zero.
            if held_since is not None:
                log.warning("porte %s : maintien interrompu apres %.0f s (%s)",
                            gate.name, now - held_since, res.detail or "inconnu")
            held_since = None

        if now - t0 >= gate.timeout:
            return GateReport(gate.name, Verdict.TIMEOUT,
                              last or "condition jamais remplie", now - t0, samples)
        await asyncio.sleep(gate.interval)


# --- sequences ---------------------------------------------------------------

@dataclass
class Stage:
    """Une etape : une action, une porte, ou les deux (action puis porte)."""
    label: str
    action: Callable[[], Awaitable[str]] | None = None
    gate: Gate | None = None


@dataclass
class StageReport:
    label: str
    verdict: Verdict
    detail: str = ""
    gate: GateReport | None = None

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.PASS, Verdict.SKIPPED)

    def __str__(self) -> str:
        mark = {"passee": "✅", "echouee": "❌", "delai depasse": "⏱️",
                "abandonnee": "🛑", "ignoree": "⏭️"}[self.verdict.value]
        detail = (self.gate.detail if self.gate is not None else "") or self.detail
        d = f" — {detail}" if detail else ""
        return f"{mark} {self.label}{d}"


@dataclass
class SequenceReport:
    name: str
    planned: list[str] = field(default_factory=list)
    stages: list[StageReport] = field(default_factory=list)
    current: str | None = None
    current_detail: str = ""
    current_elapsed: float = 0.0
    elapsed_s: float = 0.0

    @property
    def done_count(self) -> int:
        return len(self.stages)

    @property
    def ok(self) -> bool:
        return bool(self.stages) and all(s.ok for s in self.stages)

    @property
    def failed(self) -> StageReport | None:
        return next((s for s in self.stages if not s.ok), None)

    def render(self) -> str:
        """Tout le trajet, tout le temps : fait / en cours / a venir.

        Afficher les etapes a venir des le debut evite l'angoisse du message
        vide, et rend visible ce qui n'a PAS ete execute quand ca s'arrete.
        """
        out = []
        for i, label in enumerate(self.planned):
            if i < len(self.stages):
                out.append(str(self.stages[i]))
            elif i == len(self.stages) and self.current is not None:
                d = f" — {self.current_detail}" if self.current_detail else ""
                out.append(f"⏳ **{label}**{d} · {self.current_elapsed:.0f} s")
            else:
                out.append(f"⬜ {label}")
        return "\n".join(out)


async def run_sequence(
    name: str,
    stages: list[Stage],
    on_progress: Callable[[SequenceReport], Awaitable[None]] | None = None,
    cancel: asyncio.Event | None = None,
) -> SequenceReport:
    """Execute les etapes dans l'ordre et s'arrete a la premiere qui tombe."""
    rep = SequenceReport(name=name, planned=[s.label for s in stages])
    t0 = time.monotonic()

    async def emit() -> None:
        rep.elapsed_s = time.monotonic() - t0
        if on_progress is not None:
            await on_progress(rep)

    await emit()          # le trajet complet s'affiche avant la 1re action

    for stage in stages:
        if cancel is not None and cancel.is_set():
            rep.stages.append(StageReport(stage.label, Verdict.ABORTED, "annulee"))
            break

        rep.current = stage.label
        rep.current_detail = ""
        rep.current_elapsed = 0.0
        await emit()

        sr = StageReport(stage.label, Verdict.PASS)
        if stage.action is not None:
            try:
                sr.detail = await stage.action() or ""
            except Exception as exc:                    # noqa: BLE001
                sr.verdict = Verdict.FAIL
                sr.detail = f"{type(exc).__name__}: {exc}"
                log.exception("etape %s en echec", stage.label)

        if sr.ok and stage.gate is not None:
            async def sample(detail: str, elapsed: float) -> None:
                rep.current_detail = detail
                rep.current_elapsed = elapsed
                await emit()

            gr = await run_gate(stage.gate, cancel=cancel, on_sample=sample)
            sr.gate = gr
            if not gr.ok:
                sr.verdict = gr.verdict if not stage.gate.optional else Verdict.SKIPPED
                if stage.gate.optional:
                    sr.gate = GateReport(gr.gate, Verdict.SKIPPED,
                                         f"facultative — {gr.detail}", gr.elapsed_s, gr.samples)

        rep.stages.append(sr)
        rep.current = None
        rep.current_detail = ""
        await emit()
        if not sr.ok:
            log.error("sequence %s interrompue a l'etape %s", name, stage.label)
            break

    rep.elapsed_s = time.monotonic() - t0
    return rep
