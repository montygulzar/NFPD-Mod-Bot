# Privacy Policy

**Application:** NFPD Mod Bot
**Operator / Data Controller:** ombdeveloping - OMB
**Contact:** DM ombdeveloping | ID-1285998518213017663
**Last updated:** 27/07/26

This policy explains what data the Application collects, why, how long it is kept, and what rights you have over it. It applies to everyone who interacts with the Application in any Discord server where it is present.

## 1. Who is responsible for your data

ombdeveloping operates the Application and acts as the data controller for the information described below. Discord Inc. separately operates the Discord platform and has its own privacy policy, which governs your use of Discord itself.

## 2. What we store

The Application stores the following, and nothing else:

**Moderation records.** When a moderator issues a warning, mute, unmute, kick, ban, or unban, we store:
- the Discord user ID of the person the action was taken against
- the Discord user ID of the moderator who took the action
- the server (guild) ID where it happened
- the type of action
- the reason text entered by the moderator
- the date and time of the action

**Server configuration.** Per server, we store the configured mod-log channel ID, lockdown role ID, new-account alert threshold, and automatic warn-escalation thresholds.

**Scheduled actions.** For temporary bans, we store the server ID, user ID, and the time the ban should be lifted. This record is deleted once the ban expires or is manually lifted.

**Channel lock state.** When a channel is locked, we store the channel's previous permission setting so it can be restored on unlock. This record is deleted on unlock.

## 3. What we process but do not store

**Message content.** The Application uses Discord's Message Content intent so that prefix commands (for example `!kick`) work. Message content is examined in memory to detect commands and is immediately discarded. Message content is **not** written to any database, log file, or storage of any kind.

**Deleted messages.** The purge command deletes messages via Discord's API. Their content is never read into storage or retained.

**Server member information.** The Application uses Discord's Server Members intent to identify members and their roles when a command targets them. This is read live from Discord and not stored.

## 4. What we do not do

To be explicit, the Application does **not**:
- store, log, forward, or retain the content of any message, including direct messages sent to it
- read direct messages between other users (Discord's API provides no such access to any bot)
- collect email addresses, IP addresses, payment details, or any information from outside Discord
- sell, rent, or share your data with third parties for advertising or any other purpose
- use your data to build profiles for any purpose beyond the moderation features described here
- process data of anyone we know to be under the minimum age permitted by Discord's Terms of Service

## 5. Why we process this data (lawful basis)

We rely on **legitimate interests** (UK GDPR Article 6(1)(f)) to keep moderation records. The legitimate interest is maintaining a safe, rule-abiding community and giving moderators an accurate, reviewable record of enforcement decisions so those decisions can be applied consistently and appealed.

We consider this proportionate because the data is limited to the minimum needed to identify an action and its reason, it is visible only to server staff, and it is not used for any unrelated purpose.

## 6. Who can see your data

Moderation records for a server are visible to members of that server who hold moderation permissions, via the Application's commands. Records are not shared across servers except where a global action (an action applied across every server the Application is in) has been taken, in which case a record of that action exists in each affected server.

Records are also accessible to ombdeveloping as the operator, for the purposes of maintaining and troubleshooting the Application.

## 7. Where your data is stored

Data is held in a self-hosted PostgreSQL database running on a private server operated directly by ombdeveloping. It is not held by a third-party application-hosting provider.

<!-- ACTION REQUIRED: replace the bracketed text below with the country your server is
     physically located in, then delete this comment. Leaving it unspecified is a
     compliance gap, as data subjects are entitled to know the storage jurisdiction. -->

The server is located in [COUNTRY]. Where this is outside the UK or EEA, transfers are made on the basis of appropriate safeguards.

## 8. How long we keep it

- **Moderation records:** retained for an indefinite amount of time from the date of the action, then deleted, unless a moderator deletes the record sooner.
- **Scheduled action records:** deleted as soon as the action completes.
- **Channel lock records:** deleted as soon as the channel is unlocked.
- **Server configuration:** retained while the Application is a member of the server, and deleted on request after it leaves.

## 9. Your rights

Under UK GDPR you have the right to request access to the data we hold about you, correction of inaccurate data, erasure, restriction of processing, and to object to processing based on legitimate interests. You also have the right to lodge a complaint with the Information Commissioner's Office (ico.org.uk).

To exercise any of these rights, contact us at ombdeveloping, ID-1285998518213017663. We will respond within one month. You will need to provide your Discord user ID so we can locate your records.

Note that erasing a moderation record removes the record of an enforcement decision. Where we have a compelling legitimate ground to retain a record (for example, an active ban that would otherwise be unenforceable), we may refuse an erasure request and will explain why.

## 10. Security

Access to the database is restricted to the operator. The Application's credentials are stored as environment variables and are not committed to source control.

## 11. Changes to this policy

We may update this policy. Material changes will be announced in the bot description before taking effect. The "last updated" date above reflects the current version.
