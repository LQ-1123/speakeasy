package com.speakeasy.app;

import java.net.URI;
import java.text.ParsePosition;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

final class NativePolicy {
    private NativePolicy() { }
    static String socketUrl(String origin, String path, String callId) {
        URI base = URI.create(origin);
        if (!"https".equals(base.getScheme()) || base.getHost() == null || base.getUserInfo() != null
                || !path.equals("/api/v3/calls/" + callId + "/stream") || !callId.matches("[a-zA-Z0-9_-]+"))
            throw new IllegalArgumentException("Untrusted call socket");
        return "wss://" + base.getRawAuthority() + path;
    }
    static boolean trustedAudio(String origin, String audio) {
        try {
            URI base = URI.create(origin), url = URI.create(audio);
            return "https".equals(url.getScheme()) && url.getUserInfo() == null && base.getHost().equals(url.getHost())
                    && port(base) == port(url);
        } catch (Exception error) { return false; }
    }
    private static int port(URI uri) { return uri.getPort() == -1 ? 443 : uri.getPort(); }
    static boolean backHandled(String result) { return "true".equals(result); }

    static boolean validSession(String userId, String token, String expiresAt, long now) {
        if (userId == null || userId.isEmpty() || token == null || token.isEmpty() || expiresAt == null) return false;
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US);
        format.setLenient(false);
        ParsePosition position = new ParsePosition(0);
        Date expiry = format.parse(expiresAt, position);
        return expiry != null && position.getIndex() == expiresAt.length() && expiry.getTime() > now;
    }

    /**
     * Internal hash routes only. Mirrors the frontend router's top-level names so
     * a push payload cannot navigate to an arbitrary URL or an unknown page.
     */
    static boolean validRoute(String route) {
        if (route == null) return false;
        String value = route.trim();
        if (!value.startsWith("#/") || value.length() > 512) return false;
        String path = value.substring(2);
        if (path.isEmpty() || path.indexOf('\\') >= 0 || path.indexOf(' ') >= 0) return false;
        String[] parts = path.split("/", -1);
        if (parts.length == 0 || parts.length > 2) return false;
        for (String part : parts) {
            if (part.isEmpty() && parts.length > 1) return false;
            if (".".equals(part) || "..".equals(part)) return false;
        }
        String head = parts[0];
        if ("chat".equals(head) || "persona".equals(head)) {
            return parts.length == 2 && parts[1].matches("[A-Za-z0-9_%.~-]{1,128}");
        }
        if ("contacts".equals(head)) {
            return parts.length == 1 || parts[1].matches("[A-Za-z0-9_%.~-]{1,128}");
        }
        if ("settings".equals(head)) {
            return parts.length == 1 || parts[1].matches("[a-z-]{1,32}");
        }
        if ("discover".equals(head)) {
            return parts.length == 1 || "people".equals(parts[1]);
        }
        if ("chats".equals(head) || "events".equals(head) || "expressions".equals(head)
                || "me".equals(head) || "requests".equals(head)) {
            return parts.length == 1;
        }
        return false;
    }

    static String routeOr(String route, String fallback) {
        return validRoute(route) ? route.trim() : fallback;
    }

    static final class ReplyGate {
        private boolean awaitingReply;
        void utteranceSent() { awaitingReply = true; }
        boolean speechStarted() { boolean interrupt = awaitingReply; awaitingReply = false; return interrupt; }
        void finished() { awaitingReply = false; }
    }
    static final class Vad {
        private int voiceMs;
        private int silenceMs;
        private boolean speech;
        // 1 = sustained onset, 2 = end. A short transient never opens an utterance.
        synchronized int frame(double rms, int durationMs) {
            if (rms > 650) {
                voiceMs += durationMs; silenceMs = 0;
                if (!speech && voiceMs >= 80) { speech = true; return 1; }
            } else {
                voiceMs = 0;
                if (speech) {
                    silenceMs += durationMs;
                    if (silenceMs >= 700) { speech = false; silenceMs = 0; return 2; }
                }
            }
            return 0;
        }
        synchronized void reset() { voiceMs = 0; silenceMs = 0; speech = false; }
    }
}
