package com.speakeasy.app;

/**
 * Pure mapping from a semantic haptic action to a vibration pattern.
 *
 * <p>Kept free of Android imports so the mapping and its argument validation can
 * run in plain JVM unit tests. {@link MainActivity.NativeBridge#performHaptic}
 * owns the platform calls (Vibrator / VibrationEffect / system haptic switch).</p>
 */
final class HapticPolicy {
    private HapticPolicy() { }

    static final int MAX_AMPLITUDE = 255;

    /**
     * A vibration description. One-shot patterns use {@code timings.length == 1};
     * multi-step patterns use the same-index timings/amplitudes pair
     * (timings[0] is always the initial delay of 0ms).
     */
    static final class Pattern {
        private final long[] timings;
        private final int[] amplitudes;
        private final boolean waveform;

        private Pattern(long[] timings, int[] amplitudes, boolean waveform) {
            this.timings = timings;
            this.amplitudes = amplitudes;
            this.waveform = waveform;
        }

        static Pattern oneShot(long durationMs, int amplitude) {
            return new Pattern(new long[]{durationMs}, new int[]{clamp(amplitude)}, false);
        }

        static Pattern waveform(long[] timings, int[] amplitudes) {
            return new Pattern(timings.clone(), clampAll(amplitudes), true);
        }

        boolean isWaveform() { return waveform; }
        /** Timings array; never exposed mutable. */
        long[] timings() { return timings.clone(); }
        int[] amplitudes() { return amplitudes.clone(); }

        /** Total vibration duration in milliseconds (delay excluded). */
        long totalDurationMs() {
            long total = 0;
            for (int index = waveform ? 1 : 0; index < timings.length; index++) total += timings[index];
            return total;
        }

        private static int clamp(int amplitude) {
            if (amplitude < 0) return 0;
            return Math.min(amplitude, MAX_AMPLITUDE);
        }

        private static int[] clampAll(int[] values) {
            int[] copy = values.clone();
            for (int index = 0; index < copy.length; index++) copy[index] = clamp(copy[index]);
            return copy;
        }
    }

    /**
     * Semantic actions used by the frontend. Each one maps to a distinct short
     * pulse so users can feel what happened without looking at the screen.
     */
    static Pattern forType(String type) {
        if (type == null) return null;
        switch (type) {
            case "selection": return Pattern.oneShot(8, 40);
            case "toggle": return Pattern.oneShot(12, 70);
            case "longPress": return Pattern.oneShot(22, 90);
            case "recordStart": return Pattern.oneShot(16, 65);
            case "recordCancel": return Pattern.waveform(new long[]{0, 12, 40, 12}, new int[]{0, 90, 0, 60});
            case "send": return Pattern.oneShot(10, 80);
            case "save": return Pattern.oneShot(14, 70);
            case "success": return Pattern.waveform(new long[]{0, 10, 60, 16}, new int[]{0, 80, 0, 130});
            case "error": return Pattern.waveform(new long[]{0, 34, 70, 34}, new int[]{0, 150, 0, 150});
            case "callControl": return Pattern.oneShot(18, 85);
            default: return null;
        }
    }

    static boolean known(String type) { return forType(type) != null; }

    /** Used by the 24-25 fallback path which still calls the legacy vibrate(ms) API. */
    static long totalDurationMs(String type) {
        Pattern pattern = forType(type);
        return pattern == null ? 0 : pattern.totalDurationMs();
    }
}
