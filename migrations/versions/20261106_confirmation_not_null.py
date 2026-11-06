"""Reject SQL NULL in the confirmed branch of the confirmation invariant."""

from alembic import op

revision = "20261106_confirm"
down_revision = "1def6a19fcaa"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("session_confirmation", "practice_sessions", type_="check")
    op.create_check_constraint(
        "session_confirmation",
        "practice_sessions",
        "(confirmed_transcript IS NULL AND transcript_confirmed_at IS NULL) OR "
        "(confirmed_transcript IS NOT NULL AND length(trim(confirmed_transcript)) > 0 "
        "AND transcript_confirmed_at IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint("session_confirmation", "practice_sessions", type_="check")
    op.create_check_constraint(
        "session_confirmation",
        "practice_sessions",
        "(confirmed_transcript IS NULL AND transcript_confirmed_at IS NULL) OR "
        "(length(trim(confirmed_transcript)) > 0 AND transcript_confirmed_at IS NOT NULL)",
    )
