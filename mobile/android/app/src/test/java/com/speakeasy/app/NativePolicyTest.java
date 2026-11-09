package com.speakeasy.app;

import org.junit.Test;
import static org.junit.Assert.*;

public class NativePolicyTest {
    @Test public void socketUsesOnlyConfiguredOriginAndPath() {
        assertEquals("wss://api.example/api/v3/calls/c1/stream", NativePolicy.socketUrl("https://api.example", "/api/v3/calls/c1/stream", "c1"));
        assertThrows(IllegalArgumentException.class, () -> NativePolicy.socketUrl("http://api.example", "/api/v3/calls/c1/stream", "c1"));
        assertThrows(IllegalArgumentException.class, () -> NativePolicy.socketUrl("https://api.example", "//other.example", "c1"));
        assertThrows(IllegalArgumentException.class, () -> NativePolicy.socketUrl("https://api.example", "/api/v3/calls/c2/stream", "c1"));
    }
    @Test public void audioNeverLeaksTokenToAnotherOriginOrRedirect() {
        assertTrue(NativePolicy.trustedAudio("https://api.example", "https://api.example/api/audio/abc"));
        assertFalse(NativePolicy.trustedAudio("https://api.example", "https://evil.example/api/audio/abc"));
        assertFalse(NativePolicy.trustedAudio("https://api.example", "http://api.example/api/audio/abc"));
        assertFalse(NativePolicy.trustedAudio("https://api.example", "https://api.example:444/api/audio/abc"));
    }
    @Test public void backConsumesOnlyExactJavascriptBoolean() {
        assertTrue(NativePolicy.backHandled("true"));
        assertFalse(NativePolicy.backHandled("false"));
        assertFalse(NativePolicy.backHandled("\"unhandled\""));
        assertFalse(NativePolicy.backHandled("\"handled\""));
    }
    @Test public void vadEndsAnUtteranceOnceAfterSilenceAndRequiresSustainedVoice() {
        NativePolicy.Vad vad = new NativePolicy.Vad();
        for (int i = 0; i < 3; i++) assertEquals(0, vad.frame(1200, 20));
        assertEquals(1, vad.frame(1200, 20));
        for (int i = 0; i < 34; i++) assertEquals(0, vad.frame(0, 20));
        assertEquals(2, vad.frame(0, 20));
        assertEquals(0, vad.frame(0, 700));
    }
    @Test public void addendumInterruptsRecognitionBeforeAnyAudioExists() {
        NativePolicy.Vad vad = new NativePolicy.Vad();
        NativePolicy.ReplyGate reply = new NativePolicy.ReplyGate();
        for (int i = 0; i < 4; i++) vad.frame(1200, 20);
        for (int i = 0; i < 34; i++) vad.frame(0, 20);
        assertEquals(2, vad.frame(0, 20));
        reply.utteranceSent();
        for (int i = 0; i < 5; i++) vad.frame(0, 20); // 800 ms pause since speech.
        for (int i = 0; i < 3; i++) assertEquals(0, vad.frame(1200, 20));
        assertEquals(1, vad.frame(1200, 20));
        assertTrue(reply.speechStarted());
        assertFalse(reply.speechStarted());
    }
    @Test public void completedOrFailedReplyDoesNotInterruptAnUnrelatedNextUtterance() {
        NativePolicy.ReplyGate reply = new NativePolicy.ReplyGate();
        reply.utteranceSent(); reply.finished();
        assertFalse(reply.speechStarted());
        reply.utteranceSent();
        assertTrue(reply.speechStarted());
        reply.finished();
        assertFalse(reply.speechStarted());
    }
}
