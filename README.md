# Kon – Custom VC + AFK + Points bot

> **Rebrand (Leafy -> Kon):** commands `$kon`, `$konping`, `$koninfo`, `$konextra` (aliases `$kping`, `$kinfo`, `$kextra`);
> database `data/kon.db` (an old `leafy.db` is renamed automatically on first start); backups are `kon_code.zip` / `kon_data.zip`
> (old `leafy_data.zip` still works with `$backupload`); icons are uploaded as `kon_<name>` in the Yellow/Orange palette
> (the old `lf_*` green emojis can be deleted in the Developer Portal -> Emojis). Buttons of panels posted before the rebrand keep working.

## Setup
1. `pip install -r requirements.txt` (discord.py **2.5+**)
2. `.env`: `DISCORD_TOKEN=...` and `BACKUP_WEBHOOK=...` (see `.env.example`)
3. Developer Portal: **Message Content** + **Server Members** intents ON
4. Permissions: Manage Channels, Move Members, Mute Members, Manage Roles, View/Connect, Send Messages,
   Embed Links, Attach Files, Set Voice Channel Status (or Administrator). Shop roles must be **below** the bot's role.
5. `python main.py`

## Data – everything lives in `data/` (old files in the root are moved there automatically)
- `data/kon.db` (SQLite): points, progress, badges, shop, purchases, point log, point/shop/no-prefix/alias settings
- `data/vc_data.json`: custom VCs + join-to-create / interface channel ids
- `data/afk_data.json`: AFK users

## Backup (webhook only)
Set `BACKUP_WEBHOOK` in `.env`. Every backup is one small message with **two zips**:
- `kon_code.zip` - bot files, assets, `.env` (token + webhook stay in; `BACKUP_REDACT=1` removes them), `export/`
- `kon_data.zip` - the `data/` folder only (database, VC / AFK / self-role data)

Sent every 30 seconds (new message, the previous one is deleted; `BACKUP_MODE=keep` keeps all), at start and when the bot stops.
`$backup` = status, `$backup refresh` = back up now (owner).

**`$backupload`** (owner): attach one or more data zips (`kon_data.zip`, or an old full backup) and the data is merged into the
running bot by itself. New players, badges, shop items, purchases and settings are added; if a player exists in both, the bigger
value wins. Nothing is deleted. You can also reply to a message that has the zip(s).

Restore on a new machine: `python tools/restore.py kon_code.zip kon_data.zip` (verifies every file + database) →
`pip install -r requirements.txt` → `python main.py`. Check only: add `--check`.

Every backup also contains a **readable export** in `export/`: `SERVER_CONFIG.md/.json` (custom-VC join channel + panel, self-role
channel/message/roles/banner, points, shop, no-prefix, aliases, role commands, autoreact, AFK, extra owners - every ID with the
channel / role / user name next to it), `db_dump.json` (every database row) and `players_<server>.csv` (rank, points, chat, VC hours,
badges per player). `$backup config` sends SERVER_CONFIG.md + the players CSV of the current server right away.

## No-prefix & aliases
`$noprefix role @Role` (also `clear`, `add @user`, `remove @user`) · `$alias add pa vcpermit all` / `remove` / `list`
Built-in aliases: `$permit`, `$lock`, `$kick`, `$drag`, `$p` ... and `$vc kick @user` works like `$vckick @user`. Full list: `$adminhelp`.

## Commands – `$adminhelp` shows the live list with usage + aliases

## Autoreacter
`$au setup 🔥` (alias of `$autoreacter`) - the bot reacts with your emoji on every message **you** send (up to 3 emojis, `$au setup off` to stop).
Allowed: admins (Manage Server) + roles added with `$au config` (role picker menu, or `$au config add/remove @Role`, `$au config clear`).
The bot needs **Add Reactions** + **Read Message History**.

## Role commands (`$giverole`)
`$giverole setmember @Member` -> now `$setmember @person` gives that role. Works with any role/command name.
**Reply works too:** reply to a member's message and send just `$setmember` - the person you replied to gets the role (no @mention needed).
Optional 3rd role = who may use it besides **Manage Roles**: `$giverole setvip @VIP @Staff`. `$giverole list` · `$giverole remove setmember`.
The role must be below the bot's role and below yours.

## Self roles (`$editsr`)
`$setsr #channel` -> `$editsr` opens the editor (all buttons use the custom icons from `assets/icons`).
**Banner:** press *Banner* and upload a PNG / JPG / **GIF** / WEBP (max 4 MB) or paste a link - or send `$editsr` with an
image / GIF attached (or reply to one). *Remove banner* deletes it. The file type is detected from the file itself, and the
banner is re-posted under a fresh file name each time so Discord never shows a stale / empty image.

## Every server has its own configuration
Owner permission (`$konextra @friend`), no-prefix, aliases, `$giverole` commands, points, shop, autoreact, self roles, custom VC
and AFK are all stored **per server**. A friend you make owner in server A is **not** owner in server B - run `$konextra @friend`
in each server where they should have it (`$konextra list` / `remove` also work per server). Only the real bot owner can use `$konextra`.
Older backups had one global owner list; on the first start it is moved automatically to the server where that person is configured.

## Per-server backup
Besides the raw files, every backup has one folder per server: `export/servers/<server id>/`
- `config.md` / `config.json` - every setting of that server (channel / role / user names next to the IDs)
- `data.json` - that server's saved data (custom VC, self roles, panel, AFK, mutes, extra owners)
- `database.json` - every database row of that server (players, progress, badges, shop, purchases, settings, point log)

## Cleanup (`$purgebot` / `$pb`)
`$pb` removes the last 10 **bot messages + command messages** of the channel (e.g. `$afk ...` and Kon's "AFK Status Set" reply, `!ban @x`
and the other bot's answer). `$pb 100` = up to 100, max 500. Members' normal chat is never touched. Needs **Manage Messages** (you and the bot).
Messages older than 14 days can't be bulk-deleted by Discord and are skipped.

## AFK
`$afk <reason>` - reason max **50 characters**; every emoji counts as 1 (a custom emoji is not counted by the length of its `<:name:id>` code).
`$interface` no longer has the `$panel` alias.

## Autoreact without Nitro / replies
- `$ar :name:` or `$ar <emoji id>` works for members **without Nitro**: Discord only sends `:name:` for emojis of other servers, the bot looks that
  emoji up in every server it is in (and its own app emojis) and uses it. The bot needs **Use External Emojis** in the channel.
- The autoreact only fires for a real `@ping` typed in the message. Replying to someone does **not** trigger it (even with reply-ping on).

## Point shop
`$store` / `$pointshop` now show **Your Balance** (with the wallet icon) under the role list.

## New in this version
- **`$aura [@member]`** - plain message `Aura this user holds! @user:` + `[██████░░░░]` `60%`; the bar fills slowly (message is edited), dotted blocks turn white. **`$aura config`** (admins): fix aura % per member / role, random on/off.
- **`$transfer @from @to [@roles]`** - move roles between members (bot owner, `$konextra` owners and server Administrators). Shows a
  preview with Confirm / Cancel. The VC command is now only `$vctransfer` (`$vct`).
- **`$point config`** redesigned: *Main* page (chat / VC rules, switches) + *Channels* page (menus to switch points OFF in channels /
  categories, ON-only mode, voice channels, custom rules). Quick commands: `$point disable [#channel]`, `$point enable [#channel]`.
- **Tic Tac Toe** (plain-text challenge with Accept / Deny, game embed from the first move, X blue / O red, result in the message text): `/tictactoe admin #channel` (admins) sets the channel, `/tictactoe play @member` (or `$ttt play @member`) works only there. The first turn (X) is random 50-50 - the challenger does not always start.
- **BlackTea** (word-bomb game, custom Kon icons): `$blacktea [easy|medium|hard]` (alias `$bt`, no `play` needed) works only in the channel set with `$blacktea channel #channel` / `$setblacktea`; `$blacktea stop`, `$blacktea reset`.
- **`$konping`** (redesigned, custom icons) and **`$koninfo`** (servers, total members, stats; server list = bot owner only).
  `$kon` alone no longer pings, it shows a short intro.
- **`$pb`** says "Successfully purged N bot messages." and also removes the no-prefix commands (plain `afk`, `profile` ...).
- **Backup**: token + webhook stay in the zip, and the custom-VC channels (join-to-create + panel, with category) are saved in
  `export/custom_vc_channels.json` and in `data/vc_data.json -> vc_setup` (kept even if the channel is deleted).

## Update 2
- Fixed `$adminhelp` / `$koninfo` (a bad edit had cut part of `cogs/meta.py`). Help, `$koninfo`, `$konping`, `$point config`, `$aura config` embeds are smaller.
- `$pointshop config` -> **Position** button: move a role to another place in the shop.
- `$ae` / `$as`: **Undo** and **Remove** buttons under the result (also `$ae undo`, `$ae remove <emoji/name>`, `$as undo`, `$as remove <name>`).
- **`$p @member [amount]`** (aliases `$pm`, `$purgemember`): delete that member's messages (default 10, max 500). Reply to the member + `$p [amount]` works too.
  `$p` is no longer a profile alias for moderators; members without Manage Messages still get their profile with `$p` (and `$pf`, `$stats`, `$profile` work for everyone).

## Update 3 - command control + badge fix
- **`$disablecommand` / `$db`** (Manage Server): choose which command works in which channel - or in a whole **category**.
  - `$db` alone opens a menu: pick category -> commands -> channels / categories -> **Disable** / **Enable** / **List**.
  - Quick forms: `$db profile #general` · `$db badge give #chat #games` · `$db all #general` · `$db points <category ID>` ·
    `$db list` · `$db reset`. `$eb ...` (= `$enablecommand`) turns commands back on.
  - Slash: `/disablecommand` and `/enablecommand` with auto-complete for the command and a channel / category picker.
  - A disabled command answers "`$profile` is turned off in this channel." (deleted after 6 s). Turning off `badge` also turns off `badge give` / `badge remove`.
  - `$db`, `$eb`, `$adminhelp`, `$sync`, `$konextra` can never be disabled, so nobody gets locked out.
  - Stored per server in the `settings` table (key `cmd_disable`) -> part of the backup / export.
- **Badge fix**: `$badge give @user <emoji>` no longer cuts custom emojis (the old 30-character limit left `<:name:id` without `>`).
  Limit is now 100 characters, `:emoji_name:` also works, and old broken badges are repaired automatically on start.

## Update 4
- **Backup**: code and data are two separate zips, the embed is short, `$backupload` merges data zips into the live bot.
- **No reply without rights**: someone without the permission who uses an admin / owner command gets no answer at all.
- **`$aura`**: random aura is a new amount every time, the bar lands on the final value in 3 ticks.
  `$aura config` has **Disable Person** (pick a member: their aura is off; pick again: back on).
- **`$customvc config`** (Manage Server): who can't be vc-banned / kicked / muted. Server owner and Kon owners are always safe,
  admins are safe by default (toggle), and you can add any roles. `$customvc #voice` still sets the join-to-create channel.
- **Kon owners control every custom VC** (all `$vc...` commands): from the VC they sit in, or by typing the command in that VC's own chat.
- Every Kon message was rewritten shorter and lighter.

## Image links, webhook, emoji IDs
- **Image host** (admins): `$imgchannel #channel` sets a private storage channel (`$imgchannelreset` removes it). `$tolink [name]` (aliases `$link`,
  `$imglink`) turns an attached image / GIF, an image in the message you reply to, or a pasted link into a saved link (PNG/JPG/GIF/WEBP, up to 5 at once).
  `$links` lists them, `$linkget <name>` gives a fresh link + preview, `$linkdel <name>` deletes one. Only the link and the hosting message id are saved in
  `data/` (inside `kon.db`), never the file. Discord CDN links expire, so Kon re-fetches a fresh link from the hosting message whenever it needs one.
  The self-role banner is hosted in this channel too (before `ASSET_CHANNEL_ID`).
- **Webhook** (Manage Webhooks): `$webhook` opens an editor (channel, message, name, avatar, Send, Edit sent message); `$webhook #channel text` sends at once;
  `$webhookedit <message link> new text` (`$whe`) edits. Animated emojis work without Nitro: paste the emoji **ID** or `:name:` in the text.
  The avatar can be a link or the name of a saved image.
- **`$ei`**: animated emojis (of the servers you share with Kon) with their IDs, paginated. `$ei here`, `$ei <name>`, `$ei <emoji id>`.

## Update: leaderboard, webhook embeds, image uploads, expressions, VC, backup
- **`$lb`** (opens POINTS; menu switches to messages / voice) - Ordinal-style board (10 per page, `Previous` / `Find Me` / `Next`, select for Messages / Points / Voice, Kon icons).
- **`$webhook`** - editor now has **Embed** (title, text, colour, footer, author), **Embed image**, **Thumbnail**, **Avatar** (upload or link) and
  **Name**. Everything is saved per server and shown again next time. `$whe` edits the embed too.
- **Upload instead of link:** shop thumbnail, webhook avatar / embed image / thumbnail and the self-role banner all open a window with an
  upload field (link is still possible). Needs `$imgchannel` (or the channel where the button was pressed is used).
- **`$ae name`** + attach an image / GIF, or reply to a message with an image / GIF / image link. **`$as name`** same for stickers.
  Big images are shrunk automatically (needs `pillow`).
- **VC:** `$vcunmute`, `$vcdeafen`, `$vcundeafen`, `$vcmuteall`, `$vcunmuteall`, `$vcbans`, `$vcclaim`; kick / ban / unban / permit /
  mute / unmute / deafen also work as a **reply** to the member's message.
- **Backup:** `export/LINKS.md` lists every link / banner / image and where it is used; every server config now has
  an "EVERYTHING saved" section. `$backupload replace` makes the backup win over the live settings (use on a fresh bot).

- **`$setrole own <role ID>`** (same as `$giverole`) - role ID, @role or name; then `$own @user` and `$setown @user` both give the role.
