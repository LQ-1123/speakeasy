import { clientApi, currentClientIdentity, websocketUrl, type ClientBootstrap } from "./clientApi";
import { queryClient } from "../queryClient";
import { AUTH_EXPIRED_EVENT, AUTH_LOGOUT_EVENT } from "./session";
import { EventBatch } from "./eventBatch";

export interface ClientEvent { id: string; sequence: number; type: string; is_read?: boolean; replay?: boolean; friend_id?: string; post_id?: string; payload?: Record<string, unknown> }

/** One app-level stream. A short ticket is sent in the first frame, never in the URL. */
export function connectClientEvents(onEvent: (events: ClientEvent[]) => void, onSnapshot?: (snapshot: ClientBootstrap) => void): () => void {
  let stopped = false;
  let socket: WebSocket | undefined;
  let timer: number | undefined;
  let sequence = 0;
  let attempts = 0;
  const seen = new Set<string>();
  const batch = new EventBatch((events) => {
    onEvent(events);
    window.dispatchEvent(new CustomEvent("speakeasy:client-event", { detail: events }));
  });
  const connect = async () => {
    if (stopped) return;
    try {
      await queryClient.ensureQueryData({ queryKey: ["client", "bootstrap"], queryFn: clientApi.bootstrap });
      const identity = await currentClientIdentity();
      if (!identity || stopped) return;
      const cursorKey = `speakeasy.events.cursor.v1:${identity.user_id}`;
      let stored: string | null = null;
      try { stored = window.localStorage.getItem(cursorKey); } catch { /* privacy mode */ }
      const ticket = await clientApi.eventTicket();
      if (stopped) return;
      if (!sequence) sequence = stored === null ? Number(ticket.current_sequence) || 0 : Number(stored) || 0;
      if (stored === null) {
        const fresh = await clientApi.bootstrap();
        if (stopped) return;
        queryClient.setQueryData(["client", "bootstrap"], fresh);
        onSnapshot?.(fresh);
      }
      socket = new WebSocket(websocketUrl(ticket.ws_path));
      socket.onopen = () => { attempts = 0; socket?.send(JSON.stringify({ type: "auth", ticket: ticket.ticket, last_sequence: sequence || undefined })); };
      socket.onmessage = (raw) => {
        if (typeof raw.data !== "string") return;
        try {
          const event = JSON.parse(raw.data) as ClientEvent;
          if (!event.id || seen.has(event.id) || Number(event.sequence) <= sequence) return;
          seen.add(event.id);
          if (seen.size > 512) seen.delete(seen.values().next().value!);
          sequence = Math.max(sequence, Number(event.sequence) || 0);
          event.replay = event.sequence <= (Number(ticket.current_sequence) || 0);
          try { window.localStorage.setItem(cursorKey, String(sequence)); } catch { /* privacy mode */ }
          batch.add(event);
        } catch { /* Ignore non-event keepalives. */ }
      };
      socket.onclose = () => schedule();
      socket.onerror = () => socket?.close();
    } catch { schedule(); }
  };
  const schedule = () => {
    if (stopped) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void connect(), Math.min(20_000, 1000 * 2 ** attempts++));
  };
  const online = () => { socket?.close(); window.clearTimeout(timer); void connect(); };
  window.addEventListener("online", online);
  void connect();
  const stop = () => { stopped = true; batch.stop(); socket?.close(); window.clearTimeout(timer); window.removeEventListener("online", online); };
  window.addEventListener(AUTH_EXPIRED_EVENT, stop);
  window.addEventListener(AUTH_LOGOUT_EVENT, stop);
  return () => { stop(); window.removeEventListener(AUTH_EXPIRED_EVENT, stop); window.removeEventListener(AUTH_LOGOUT_EVENT, stop); };
}
