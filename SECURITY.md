# Security scope

Initial support: macOS only. This is a trusted local/LAN development gateway, not a hardened public multi-tenant service.

The foreground CLI defaults to loopback; the LaunchAgent binds to 0.0.0.0. Without MEDIA_API_KEY, private IPv4/LAN and loopback clients can access task APIs. With MEDIA_API_KEY configured, all task API clients need Bearer authentication, including LAN. The current Web UI has no login flow. Health and static pages remain public. Configure TLS and an appropriate access boundary before any use outside a trusted LAN.

Engine profiles and scripts are trusted administrator code. They can execute with the service user's permissions. There is no API for untrusted users to upload plugins. Do not put secrets in profile env/options: execution snapshots are inspectable. The service is not an OS sandbox, and offline environment flags do not prevent a custom engine from networking.

Do not commit keys, plist files, tasks, user prompts, generated media or full logs. Public release requires provenance/license review and a secret scan. Result paths are checked for task-directory containment, but this does not make untrusted engines safe.

No public release, security SLA or verified private reporting address exists yet. Report issues privately to the repository owner. Establish a private vulnerability-reporting channel before publication.
