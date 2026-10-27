# An @ of a 群友 names their 主账号, gated by the binding alone

`账号`, `趋势` and `玩家排名 <…>` accept an @ of a 群友 as a fourth form of
账号引用, and draw that person's 主账号 (2026-10-27). The only thing the
lookup checks is that the platform user has a binding: not that they have
used the bot in this chat, not an opt-out flag. A binding ties a person to a
*public* account, every figure the page draws is on the site already, and
`解绑` is the way out; a second consent would be a second concept for the
same fact. Bindings already answer in private chats (#36), and on the QQ
official bot one openid is the same in every group, so the lookup is
cross-group by nature.

## Considered Options

- **The old 群榜 membership rule** — a bound user is visible in a chat only
  after using a binding command there. Rejected: "TA 明明绑了却查不到" would
  be a routine reply, and the concept left with 群榜 in 1.3.0 (`3fcef6d`).
- **An opt-out flag per user.** Rejected: one more command and one more
  state for a request nobody has made; `解绑` already is the opt-out.
- **Finding a 群友 by group card or nickname** where the platform does not
  deliver who was mentioned. Rejected: `bindings.json` holds no QQ nickname
  (CLAUDE.md invariant), and keeping one to make this work would undo it.

## Consequences

The QQ official bot is supported because the platform delivers the
mentioned member's `member_openid` in the group event's `mentions`, which
equals that member's own sender id — measured on 2026-10-27, not only
documented. AstrBot does not turn it into an At component there, so the
plugin reads it itself, in `qq_official`; when AstrBot does, that reader goes.
