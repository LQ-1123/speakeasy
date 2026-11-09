package com.speakeasy.app;

import java.net.URI;

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
