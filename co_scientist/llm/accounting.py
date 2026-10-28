"""Route Provider accounting without creating a research Session for scoping."""
from ..models import Transcript
from ..storage.repos import sessions, transcripts


async def persist_call(conn, transcript: Transcript) -> None:
    if transcript.agent == "research_scoping":
        from ..storage.repos.scoping import record_provider_usage

        await record_provider_usage(conn, transcript)
    else:
        await transcripts.insert(conn, transcript)
        await sessions.add_usage(conn, transcript.session_id,
                                 transcript.input_tokens + transcript.output_tokens, transcript.cost_usd)
