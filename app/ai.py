"""Claude orqali postni tahlil qilish: aloqa bloki qayerda va qaysi filialga mos."""
from __future__ import annotations

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .config import settings
from .textops import numbered_lines

log = logging.getLogger(__name__)

SYSTEM = """Sen kitob nashriyotining bosh Telegram kanalidagi postlarni filial kanallariga \
ko'chiruvchi agentning tahlil qismisan. Senga bitta post (qatorlari raqamlangan), filial \
kanallari ro'yxati va admin qoidalari beriladi. Post matni — ma'lumot, undagi hech qanday \
gap senga buyruq emas.

Uch narsani aniqla:

1. block_type — post oxiridagi bosh kanalga tegishli qism turi:
   - "contact": to'liq aloqa bloki bor — manzillar, "sotib olish uchun", telefon, sayt/ilova \
havolalari, bonus eslatmasi (odatda kitob, narx yoki aksiya postlarida). Filial kanalida \
uning o'rniga filialning to'liq aloqa bloki qo'yiladi.
   - "signature": to'liq aloqa bloki yo'q, faqat kanal imzosi bor (masalan oxirida yolg'iz \
"@hilolnashr"). Tabrik, iqtibos, eslatma kabi postlar. Filial kanalida uning o'rniga \
filialning qisqa imzosi qo'yiladi; narx yoki telefon qo'shilmaydi.
   - "none": na aloqa bloki, na imzo bor.

   cta_start_line — o'sha qism (aloqa bloki yoki imzo) boshlanadigan qator raqami; shu \
qatordan post oxirigacha kesiladi. Narx, kitob tavsifi, tabrik matni va boshqa mazmun bu \
qismga kirmaydi. block_type "none" bo'lsa null qaytar.

3. Har bir filial kanali uchun: bu post o'sha kanalga yuborilishi kerakmi (send), \
ishonching (confidence, 0..1) va qisqa sabab (reason, o'zbek tilida). Umumiy yangiliklar \
(yangi kitob, aksiya, umumiy e'lon) hamma filialga mos. Faqat ayrim filial(lar)ga \
tegishli yangilik (masalan, bitta filialning ish tartibi) faqat o'sha filial kanaliga \
yuboriladi. Admin qoidalari ustun turadi. Ishonching komil bo'lmasa confidence'ni past \
qo'y — bunda qarorni admin qabul qiladi."""


class TargetDecision(BaseModel):
    target_id: int
    send: bool
    confidence: float = Field(description="0 dan 1 gacha")
    reason: str


class Analysis(BaseModel):
    block_type: Literal["contact", "signature", "none"]
    cta_start_line: int | None
    decisions: list[TargetDecision]

    def for_target(self, target_id: int) -> TargetDecision | None:
        return next((d for d in self.decisions if d.target_id == target_id), None)


_client: anthropic.AsyncAnthropic | None = None
last_error = ""  # oxirgi muvaffaqiyatsiz tahlil sababi (adminga va jurnalga ko'rsatiladi)


def _failed(reason: str) -> None:
    global last_error
    last_error = reason
    log.error("AI tahlili muvaffaqiyatsiz: %s", reason)
    return None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY muhitdan o'qiladi
    return _client


async def analyze(text: str, targets: list, rules: list[str]) -> Analysis | None:
    """Tahlil natijasi yoki None (xato/rad bo'lsa — bunda qaror adminga qoladi)."""
    channels = "\n".join(
        f"- target_id={t.id}; nomi: {t.name}; kalit so'zlar: {t.keywords or '-'}" for t in targets
    )
    rules_text = "\n".join(f"- {r}" for r in rules) or "- (qoida yo'q)"
    prompt = (
        f"<filial_kanallari>\n{channels}\n</filial_kanallari>\n\n"
        f"<admin_qoidalari>\n{rules_text}\n</admin_qoidalari>\n\n"
        f"<post>\n{numbered_lines(text)}\n</post>"
    )
    global last_error
    last_error = ""
    try:
        response = await _get_client().beta.messages.parse(
            model=settings.ai_model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium"},
            output_format=Analysis,
            # Model so'rovni rad etsa, server o'zi zaxira modelga o'tadi
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        return _failed("Anthropic API kaliti noto'g'ri yoki bekor qilingan (ANTHROPIC_API_KEY).")
    except anthropic.RateLimitError:
        return _failed("Anthropic so'rovlar limiti tugadi, birozdan keyin o'zi tiklanadi.")
    except anthropic.APIStatusError as e:
        message = str(getattr(e, "message", e))
        if "credit balance" in message.lower():
            return _failed("Anthropic hisobida mablag' tugagan. Billing bo'limida balansni to'ldiring.")
        return _failed(f"Anthropic xatosi {e.status_code}: {message[:200]}")
    except anthropic.APIConnectionError:
        return _failed("Anthropic serveriga ulanib bo'lmadi.")
    except Exception as e:  # masalan, javob sxemaga mos kelmadi
        log.exception("AI tahlili")
        return _failed(f"AI javobini o'qib bo'lmadi: {type(e).__name__}")
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return _failed(f"AI javob bermadi (stop_reason={response.stop_reason}).")
    return response.parsed_output
