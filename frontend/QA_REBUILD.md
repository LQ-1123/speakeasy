# Mobile rebuild frontend verification

Verified 2026-11-09 against the production Next.js static export. Visual tests used `scripts/qa-server.mjs` deterministic local fixtures and `scripts/qa-native-bridge.js`; no real account, external model, push provider, or physical Android device was used.

- `npm run typecheck`: passed.
- `npm test`: 22 tests passed across 9 files. Covers route nesting/back dispatch, history merge, ordered playback/invalidation, speech gating, native session restoration, authenticated browser handshake/capture cancellation, mute reset, interruption during pending recognition, identity refresh, replay batching, and global notification gating.
- `npm run build`: passed; static export in `out/`.
- `git diff --check -- frontend`: passed (line-ending advisories only).
- Browser viewport widths 320, 360, 390, 430: document width matched viewport width; no horizontal overflow on the messages page.
- 90 friends: 20 rendered conversation rows. 1,000 fixture messages: 10 rendered message rows. 1,000 fixture posts: 5 rendered feed cards.
- Message list scroll: 2,200 px before switching tabs, 2,200 px after returning.
- Native-fixture call: start, minimize, expand, mute, speaker and end controls; system back navigates to messages while call remains minimized. Minimized strip bottom 95.84 px, header top 100 px (no overlap). Corrected total-height accounting: app/composer bottom 844 px, editor bottom 832 px at an 844 px viewport.
- Light/dark settings visually inspected. Synthetic doubling of text font sizes in settings at 390 px retained a 390 px document width.

Screenshots are local QA artifacts in `C:/Users/Administrator/.agent-browser/tmp/screenshots/`:

| View | File |
| --- | --- |
| Messages | screenshot-1794228782471.png |
| Chat without repeated intro | screenshot-1794228993653.png |
| Full call | screenshot-1794229011454.png |
| Minimized call with visible composer | screenshot-1794229376116.png |
| Light settings | screenshot-1794228134819.png |
| Dark settings | screenshot-1794228112288.png |
| Me | screenshot-1794228135147.png |
| Feed | screenshot-1794228135724.png |
| Synthetic 200% text settings | screenshot-1794229058545.png |

Physical-device echo cancellation, microphone/audio route changes, OEM background survival, live push delivery and vendor credentials remain native/device acceptance work. Browser microphone hardware and actual service audio were tested through deterministic unit adapters, not a live call provider. The domain store uses a stable Zustand store with shallow selectors behind the compatibility provider; Query v5 owns bootstrap/history caching. Long message/feed rows are virtualized.
