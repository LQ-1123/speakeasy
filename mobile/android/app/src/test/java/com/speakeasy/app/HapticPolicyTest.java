package com.speakeasy.app;

import org.junit.Test;

import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.junit.Assert.*;

/** JVM tests for the semantic haptic mapping used by NativeBridge.performHaptic. */
public class HapticPolicyTest {
    private static final List<String> TYPES = Arrays.asList(
            "selection", "toggle", "longPress", "recordStart", "recordCancel",
            "send", "save", "success", "error", "callControl");

    @Test public void everySemanticActionMapsToAShortBoundedPattern() {
        for (String type : TYPES) {
            HapticPolicy.Pattern pattern = HapticPolicy.forType(type);
            assertNotNull("missing mapping for " + type, pattern);
            assertTrue("duration must be positive for " + type, pattern.totalDurationMs() > 0);
            assertTrue("haptic must stay short for " + type, pattern.totalDurationMs() <= 200);
            assertEquals(pattern.timings().length, pattern.amplitudes().length);
            for (int amplitude : pattern.amplitudes()) {
                assertTrue("amplitude in range for " + type, amplitude >= 0 && amplitude <= HapticPolicy.MAX_AMPLITUDE);
            }
            for (long timing : pattern.timings()) assertTrue("timing must not be negative for " + type, timing >= 0);
        }
    }

    @Test public void actionsAreDistinguishable() {
        Set<String> signatures = new HashSet<>();
        for (String type : TYPES) {
            HapticPolicy.Pattern pattern = HapticPolicy.forType(type);
            signatures.add(Arrays.toString(pattern.timings()) + Arrays.toString(pattern.amplitudes()));
        }
        assertEquals("each semantic action needs its own pattern", TYPES.size(), signatures.size());
    }

    @Test public void recordCancelAndErrorHaveDistinctRumbleInsteadOfASinglePulse() {
        HapticPolicy.Pattern cancel = HapticPolicy.forType("recordCancel");
        HapticPolicy.Pattern error = HapticPolicy.forType("error");
        assertTrue(cancel.isWaveform());
        assertTrue(error.isWaveform());
        assertEquals(0L, cancel.timings()[0]);
        assertEquals(0L, error.timings()[0]);
        assertTrue(cancel.totalDurationMs() > 0);
        assertTrue(error.totalDurationMs() > cancel.totalDurationMs());
    }

    @Test public void unknownOrMissingTypesAreIgnored() {
        assertNull(HapticPolicy.forType(null));
        assertNull(HapticPolicy.forType(""));
        assertNull(HapticPolicy.forType("vibrateEverything"));
        assertFalse(HapticPolicy.known("selection2"));
        assertEquals(0L, HapticPolicy.totalDurationMs("unknown"));
    }

    @Test public void knownTypeIsRecognised() {
        for (String type : TYPES) assertTrue(HapticPolicy.known(type));
    }
}
