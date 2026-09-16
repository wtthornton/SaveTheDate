"""tap-7739 per-person attendees, segments, rsvp phases

Revision ID: 155528dee388
Revises: 34c3f17d487b
Create Date: 2026-09-16 13:52:45.508288

`events` and `guests` keep their primary keys and `guests.invite_token` is never
touched, so every invite link already in the wild keeps working.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "155528dee388"
down_revision: str | Sequence[str] | None = "34c3f17d487b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `attendees.attending` ("coming to anything") is kept deliberately alongside the
# per-segment `attendance` rows, so it needs enforcing rather than trusting. The
# trigger is a CONSTRAINT trigger and INITIALLY DEFERRED because a single RSVP
# writes the attendee first and its segments immediately after — the pair is only
# consistent at COMMIT.
#
# The rule is suspended for an event with no segments on its schedule: there,
# `attending` stands alone and has nothing to disagree with.
_CONSISTENCY_FUNCTION = """
CREATE OR REPLACE FUNCTION savethedate_attendee_attendance_consistent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target uuid;
    declared boolean;
    attends_something boolean;
    event_has_schedule boolean;
BEGIN
    IF TG_TABLE_NAME = 'attendees' THEN
        target := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.attendee_id;
    ELSE
        target := NEW.attendee_id;
    END IF;

    SELECT a.attending INTO declared FROM attendees a WHERE a.id = target;
    IF NOT FOUND THEN
        -- The attendee was removed later in the same transaction.
        RETURN NULL;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM segments s
        JOIN guests g ON g.event_id = s.event_id
        JOIN attendees a ON a.guest_id = g.id
        WHERE a.id = target
    ) INTO event_has_schedule;

    IF NOT event_has_schedule THEN
        RETURN NULL;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM attendance x WHERE x.attendee_id = target AND x.attending
    ) INTO attends_something;

    IF declared AND NOT attends_something THEN
        RAISE EXCEPTION 'attendee % is marked attending but attends no segment', target
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT declared AND attends_something THEN
        RAISE EXCEPTION 'attendee % is marked not attending but attends a segment', target
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$;
"""

_CREATE_TRIGGERS = """
CREATE CONSTRAINT TRIGGER attendees_attendance_consistent
AFTER INSERT OR UPDATE ON attendees
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION savethedate_attendee_attendance_consistent();

CREATE CONSTRAINT TRIGGER attendance_attendee_consistent
AFTER INSERT OR UPDATE OR DELETE ON attendance
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION savethedate_attendee_attendance_consistent();
"""

# One attendee per seat the old invitation-level RSVP claimed. The first takes the
# invitation's name; the rest are placeholders the guest renames when they next
# open their link. `party_size = 0` (a decline) produces no attendees at all.
_BACKFILL_ATTENDEES = """
INSERT INTO attendees (id, guest_id, name, attending, is_child, dietary_tags, created_at)
SELECT
    gen_random_uuid(),
    g.id,
    CASE WHEN seat.n = 1 THEN g.name ELSE g.name || ' (guest ' || seat.n || ')' END,
    r.attending,
    false,
    '{}'::text[],
    now()
FROM rsvps r
JOIN guests g ON g.id = r.guest_id
CROSS JOIN LATERAL generate_series(1, GREATEST(r.party_size, 0)) AS seat(n);
"""

# A date deadline of 2027-12-15 means "the end of that day where the wedding is",
# so it becomes the instant the day after starts, in the event's own time zone.
_DEADLINE_TO_TIMESTAMP = "(rsvp_deadline + INTERVAL '1 day')::timestamp AT TIME ZONE \"timezone\""

_DEADLINE_TO_DATE = "(rsvp_deadline AT TIME ZONE \"timezone\" - INTERVAL '1 day')::date"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "events",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )
    op.add_column("events", sa.Column("rsvp_opens_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        "events",
        "rsvp_deadline",
        existing_type=sa.Date(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using=_DEADLINE_TO_TIMESTAMP,
    )

    op.create_table(
        "segments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("is_optional", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("booking_url", sa.String(length=500), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_segments_event_id"), "segments", ["event_id"], unique=False)

    op.create_table(
        "attendees",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("guest_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("attending", sa.Boolean(), nullable=False),
        sa.Column("is_child", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "dietary_tags",
            sa.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("dietary_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_attendees_guest_id"), "attendees", ["guest_id"], unique=False)

    op.create_table(
        "attendance",
        sa.Column("attendee_id", sa.UUID(), nullable=False),
        sa.Column("segment_id", sa.UUID(), nullable=False),
        sa.Column("attending", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["attendee_id"], ["attendees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attendee_id", "segment_id"),
    )

    op.execute(_BACKFILL_ATTENDEES)

    # Both are now derived from `attendees`, which removes the drift between a
    # stored count and who is actually coming.
    op.drop_column("rsvps", "attending")
    op.drop_column("rsvps", "party_size")

    op.execute(_CONSISTENCY_FUNCTION)
    op.execute(_CREATE_TRIGGERS)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS attendance_attendee_consistent ON attendance;")
    op.execute("DROP TRIGGER IF EXISTS attendees_attendance_consistent ON attendees;")
    op.execute("DROP FUNCTION IF EXISTS savethedate_attendee_attendance_consistent();")

    op.add_column("rsvps", sa.Column("attending", sa.Boolean(), nullable=True))
    op.add_column("rsvps", sa.Column("party_size", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE rsvps r SET
            attending = EXISTS (
                SELECT 1 FROM attendees a WHERE a.guest_id = r.guest_id AND a.attending
            ),
            party_size = (
                SELECT count(*) FROM attendees a WHERE a.guest_id = r.guest_id AND a.attending
            );
        """
    )
    op.alter_column("rsvps", "attending", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("rsvps", "party_size", existing_type=sa.Integer(), nullable=False)

    op.drop_table("attendance")
    op.drop_index(op.f("ix_attendees_guest_id"), table_name="attendees")
    op.drop_table("attendees")
    op.drop_index(op.f("ix_segments_event_id"), table_name="segments")
    op.drop_table("segments")

    op.alter_column(
        "events",
        "rsvp_deadline",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.Date(),
        existing_nullable=True,
        postgresql_using=_DEADLINE_TO_DATE,
    )
    op.drop_column("events", "rsvp_opens_at")
    op.drop_column("events", "timezone")
