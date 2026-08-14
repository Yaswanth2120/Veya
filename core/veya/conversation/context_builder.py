"""Builds the LLM prompt from session context + a detected question, plus
(Section 9) an optional bounded block of retrieved document context. Pure
string assembly — no I/O, no logging, easily testable without a real LLM.
The prompt asks the model to answer in a fixed, delimited format so
`answer_generation.parse_answer_text` can deterministically extract a
short answer, talking points, and an optional caveat.
"""

from __future__ import annotations

from .models import SessionContext

_RESPONSE_FORMAT_INSTRUCTIONS = """Answer as the candidate, speaking directly to the interviewer — first person, out loud, right now. Do not think out loud, show your reasoning, or plan before answering; begin speaking your response immediately.

Respond in exactly this format, with no extra commentary before or after, and nothing before the ANSWER line — no "Here is an answer", "Based on your resume", "Certainly", "Sure", or any other preamble:

ANSWER: <the natural, speakable answer, starting with the first real word of what the candidate would say — several sentences if the question warrants it, never a clipped one-liner and never just a label for the points below. No bullets, no markdown headings, no citations or source excerpts inside this line.>
POINTS:
- <an optional supporting detail, only if it adds something the natural answer didn't already cover>
- <another optional supporting detail>
CAVEAT: <a brief caveat or clarifying assumption, or "none">

The ANSWER line is the primary content and must stand on its own as something a person could say aloud verbatim — never a rigid list of fragments, and never just a topic label. POINTS is genuinely optional expandable detail, not a restatement of the answer; leave it as just "POINTS:" with no bullets if nothing further is useful. Do not invent citations, sources, or documents. Never invent experience, employers, metrics, projects, or technologies not present in the context above. If you are not certain, say so in the caveat rather than fabricating specifics."""

_GROUNDED_INSTRUCTIONS = """Use the supporting context above only for claims specific to the user's documents — do not treat it as ground truth for anything else, and do not quote or cite it beyond what's needed to answer. If your answer would conflict with the supporting context, say so explicitly in the caveat rather than picking one silently."""

_CODING_INSTRUCTIONS = """This is a coding-practice session. Give a correct, incremental solution; explain trade-offs, tests, edge cases, and time/space complexity. Do not claim that code was executed unless an execution result was provided."""
_SYSTEM_DESIGN_INSTRUCTIONS = """This is a system-design session. State assumptions, requirements, components, data flow, failure modes, scaling, security, observability, and trade-offs. Keep the answer implementable and explicit about uncertainty."""


def render_prompt(
    session_context: SessionContext,
    question_text: str,
    document_context_block: str = "",
    memory_context_block: str = "",
    recent_conversation_block: str = "",
    user_answer_block: str = "",
) -> str:
    """Assembles a minimal structured prompt from the session's own
    fields (nothing external — see build prompt non-goals) plus the
    detected question, and — when retrieval found relevant document
    chunks for this session — `document_context_block` (see
    `knowledge.retrieval.KnowledgeRetriever.build_context_block`, already
    bounded/truncated before it ever reaches here). Every included field
    is optional/blank-safe: an empty `SessionContext` and no document
    context still produce a valid prompt using only the question."""
    context_lines = []
    if session_context.title:
        context_lines.append(f"Session: {session_context.title}")
    if session_context.company:
        context_lines.append(f"Company: {session_context.company}")
    if session_context.role_or_topic:
        context_lines.append(f"Role/Topic: {session_context.role_or_topic}")
    if session_context.description:
        context_lines.append(f"Description: {session_context.description}")
    if session_context.notes:
        context_lines.append(f"Notes: {session_context.notes}")
    if session_context.preferred_programming_language:
        context_lines.append(f"Preferred language: {session_context.preferred_programming_language}")
    if session_context.custom_instructions:
        context_lines.append(f"Custom instructions: {session_context.custom_instructions}")

    style_line = ""
    if session_context.preferred_answer_style:
        style_line = f"Preferred answer style: {session_context.preferred_answer_style}\n"

    session_block = "\n".join(context_lines)
    if session_block:
        session_block = f"Context:\n{session_block}\n\n"

    memory_block = ""
    if memory_context_block:
        memory_block = f"Remembered about this user (from previously approved memory, may not all be relevant):\n{memory_context_block}\n\n"

    # A spoken question is frequently split across Whisper windows. Include
    # only the small, immediately preceding local transcript context so the
    # answer model can resolve pronouns and the rest of the interviewer's
    # request without turning every answer into a summary of the session.
    conversation_block = ""
    if recent_conversation_block:
        conversation_block = f"Recent live conversation (use only when relevant):\n{recent_conversation_block}\n\n"

    # Section 16 (separated-track interview mode): the user's own most
    # recent *actual spoken answer*, captured from their microphone as
    # authoritative — never one of Veya's own prior suggestions, which are
    # deliberately never remembered as conversation context anywhere. A
    # follow-up question must ground itself in what the user really said,
    # not in what Veya guessed they might say.
    user_answer_section = ""
    if user_answer_block:
        user_answer_section = (
            f"The user's own most recent actual answer, spoken live (ground any follow-up in this, "
            f"not in any prior suggestion):\n{user_answer_block}\n\n"
        )

    document_block = ""
    grounded_instructions = ""
    if document_context_block:
        document_block = f"{document_context_block}\n\n"
        grounded_instructions = f"{_GROUNDED_INSTRUCTIONS}\n\n"

    mode_instructions = ""
    if session_context.session_type == "codingPractice":
        mode_instructions = f"{_CODING_INSTRUCTIONS}\n\n"
    elif session_context.session_type == "systemDesign":
        mode_instructions = f"{_SYSTEM_DESIGN_INSTRUCTIONS}\n\n"

    return (
        f"{session_block}"
        f"{memory_block}"
        f"{conversation_block}"
        f"{user_answer_section}"
        f"{style_line}"
        f"A question was just asked in this live conversation:\n"
        f"\"{question_text}\"\n\n"
        f"{document_block}"
        f"{grounded_instructions}"
        f"{mode_instructions}"
        f"{_RESPONSE_FORMAT_INSTRUCTIONS}"
    )
