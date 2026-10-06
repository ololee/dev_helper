# DevHelper relay v1

The phone and computer make outgoing HTTP(S) connections. No device needs a public IP or an inbound router port. The Linux service queues requests and temporarily streams files; it never executes scripts or tools itself.

No accounts or password login are required. `POST /api/relay/workspaces {}` creates a random UUID `workspaceId` (displayed as a connection code). Both devices enter the same code. All remaining endpoints require `X-DevHelper-Workspace: <workspaceId>`; the code is a private capability, not a public catalog identifier. Public deployments use HTTPS; localhost/private development may use HTTP. Connection codes and server settings stay in private device configuration, outside published source and knowledge sync.

## Device and catalog

- `POST /api/relay/register` and `/api/relay/heartbeat`: `{deviceId: UUID, platform: "android"|"mac", name: string, lanAddresses: [HTTP roots], sameLan: boolean, catalog?: {documents: [metadata], attachments: [metadata], tasks: [metadata]}}`. Returns `{deviceId, serverTime}`. Device online status expires after 75 seconds without heartbeat. Catalogs contain titles/IDs/kinds/revisions/hashes/sizes/status only, never document bodies, transcript text, clipboard text, secrets or binary content.
- `GET /api/relay/devices`: `{devices: [{deviceId, platform, name, lanAddresses, sameLan, online, lastSeen}]}`.
- `GET /api/relay/catalog/{deviceId}`: `{deviceId, catalog, updatedAt, online}`. Catalog remains available offline. Catalog upload never sends recording bytes.

## Outgoing request queue

- `POST /api/relay/requests`: `{id: UUID, sourceId: UUID, targetId: UUID, method: "GET"|"HEAD"|"POST"|"DELETE", path: relative API path with optional query, headers?: {"content-type": string}, body?: JSON, blobId?: UUID}`. Allowed paths: `/api/knowledge/...`, `/api/workflows/...`, `/mcp`; no arbitrary host URL. Client-generated IDs are idempotent: a repeated identical request returns the original; a different payload with the same ID conflicts. Response is `{id,state:"pending"|"delivered"|"completed"|"failed"|"cancelled",...}`.
- `GET /api/relay/inbox?deviceId=<UUID>&wait=25`: `{requests: [request]}`. Claim pending requests once; a claimed operation is never automatically replayed after restart or timeout. Only the target can reply. Long polling uses HTTP, not stdio or ADB.
- `POST /api/relay/replies/{id}`: `{deviceId: targetUUID, status: HTTPstatus, headers?: {"content-type": string}, body?: JSON, blobId?: UUID}`. Persist the reply before acknowledging it.
- `GET /api/relay/requests/{id}?sourceId=<UUID>&wait=25`: request state, and `response` when completed. Response is `{status,headers,body?,blobId?}`. Pending/accepted is not task success. Unknown delivery becomes failed and needs an explicit new request.
- `DELETE /api/relay/requests/{id}?sourceId=<UUID>` cancels a queued request. Delivered operations may already have happened; report that uncertainty rather than replaying or pretending to undo them.

## Streaming files

- `POST /api/relay/blobs?deviceId=<UUID>&name=<displayName>`: stream raw bytes with the real Content-Type, returns `{id,bytes,sha256,mimeType,name}`. No arbitrary small media-size quota; clients must stream instead of buffering full files.
- `GET /api/relay/blobs/{id}` and `HEAD`: raw bytes, with Range downloads for resume; `DELETE` removes a temporary transfer. Blob access is restricted to the workspace. Store atomically and validate SHA256 before importing to the destination attachment library. Unfinished uploads are not visible. Completed transfer blobs expire after 24 hours and are not a permanent recording archive.

## Routing and user control

The phone's “same local network” switch controls LAN eligibility. OFF always uses relay. ON tries fresh advertised/discovered LAN addresses, validates peer identity, and falls back to relay on connection failure; an IP prefix alone is not proof. Network changes invalidate stale LAN decisions and refresh advertised addresses. All catalog updates are automatic while connected. Content, recording files and clipboard text move only on an explicit transfer/sync action; connecting or refreshing the list never uploads them. A computer's explicit synchronize action fetches metadata and then the requested content from the online phone through this route. Offline requests remain queued; show queue state honestly.

Clients expose an HTTP transport adapter so existing semantic-CAS knowledge sync, task submission, attachment imports and MCP calls retain their validation and conflict behavior. Desktop background auto-sync may refresh relay catalogs but must not automatically fetch recording/media content through relay. LAN background synchronization must also honor the new manual media transfer policy. Clipboard auto-share is opt-in and disabled by default; sending current clipboard is a separate explicit button.

## Direct execution and MCP gateway

Local peer `GET /api/relay/identity` requires the same connection-code header and returns `{deviceId,platform}`. Validate the exact registered UUID before direct transfers. Local `POST /api/relay/execute` receives the same request envelope and uses a persistent UUID journal shared with relay inbox delivery. A lost response after submission is `delivery_unknown`; never automatically re-execute a mutating request through another route.

The Linux `POST /devices/{targetId}/mcp` endpoint forwards JSON-RPC over the same mailbox. Set `X-DevHelper-Workspace` and `X-DevHelper-Source` (registered source device UUID). Devices internally request `/mcp` with `Accept: application/json, text/event-stream`. Responses are JSON, notifications return HTTP 202. If a response is unavailable within 120 seconds the JSON-RPC error contains the durable request ID/state; inspect it instead of blindly repeating tool calls. The gateway uses stateless HTTP MCP and never runs tools on Linux.
