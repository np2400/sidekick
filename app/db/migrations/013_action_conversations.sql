-- 013: approvals belong to a conversation (W5 in docs/web-architecture.md). Additive: one new table only.
-- On the web several conversations are open at once, so a typed "yes" must approve only the proposal made in
-- that conversation. A proposal made in a web conversation is linked to it here; a proposal with no row here
-- belongs to Telegram's chat. Buttons name their proposal, so they work from anywhere (same account only).

CREATE TABLE action_conversations (
    action_id        INTEGER PRIMARY KEY REFERENCES pending_actions (id),
    conversation_id  INTEGER NOT NULL      -- no foreign key: a conversation can be deleted (/forget) before this
);
CREATE INDEX action_conversations_by_conversation ON action_conversations (conversation_id);
