package com.speakeasy.app;

import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

import java.util.ArrayDeque;
import java.util.Deque;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    private final Handler ui = new Handler(Looper.getMainLooper());
    private WebView wv;

    /* 通话语音串行播放：避免多句同时出声（第一轮 TTS 很快时尤其明显） */
    private final Deque<String> callQueue = new ArrayDeque<>();
    private MediaPlayer callPlayer;
    private boolean callPlaying;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            WebView webView = getBridge() != null ? getBridge().getWebView() : null;
            if (webView != null) {
                wv = webView;
                WebSettings s = webView.getSettings();
                s.setMediaPlaybackRequiresUserGesture(false);
                s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

                webView.addJavascriptInterface(new Object() {
                    @JavascriptInterface
                    public void ding() {
                        playRaw(R.raw.notify_message);
                    }

                    @JavascriptInterface
                    public void dong() {
                        playRaw(R.raw.notify_request);
                    }

                    /** 原生播放在线音频：进入队列，按顺序逐句播放 */
                    @JavascriptInterface
                    public void playUrl(final String url) {
                        if (url == null) return;
                        if (!url.startsWith("http://") && !url.startsWith("https://")) {
                            notifyJs("bad-url");
                            return;
                        }
                        ui.post(
                                () -> {
                                    if (callQueue.size() > 8) callQueue.clear();
                                    callQueue.addLast(url);
                                    if (!callPlaying) playNext();
                                });
                    }

                    /** 通话结束：停止并清空 */
                    @JavascriptInterface
                    public void stop() {
                        ui.post(
                                () -> {
                                    callQueue.clear();
                                    stopPlayer();
                                });
                    }
                }, "AndroidTone");
            }
        } catch (Throwable ignored) {
            // 设置失败不影响 App 启动
        }
    }

    private void playNext() {
        String url = callQueue.pollFirst();
        if (url == null) {
            callPlaying = false;
            notifyJs("native-done");
            return;
        }
        callPlaying = true;
        stopPlayer();
        try {
            final MediaPlayer mp = new MediaPlayer();
            callPlayer = mp;
            mp.setAudioAttributes(
                    new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build());
            mp.setDataSource(url);
            mp.setVolume(1.0f, 1.0f);
            mp.setOnPreparedListener(
                    m -> {
                        m.start();
                        notifyJs("native-start");
                    });
            mp.setOnCompletionListener(
                    m -> {
                        m.release();
                        if (callPlayer == m) callPlayer = null;
                        playNext();
                    });
            mp.setOnErrorListener(
                    (m, what, extra) -> {
                        m.release();
                        if (callPlayer == m) callPlayer = null;
                        notifyJs("native-error:" + what + "/" + extra);
                        playNext();
                        return true;
                    });
            mp.prepareAsync();
        } catch (Throwable t) {
            notifyJs("native-ex:" + t.getClass().getSimpleName());
            playNext();
        }
    }

    private void stopPlayer() {
        if (callPlayer != null) {
            try {
                callPlayer.stop();
            } catch (Throwable ignored) {
                // 已停止/未开始
            }
            try {
                callPlayer.release();
            } catch (Throwable ignored) {
                // 已释放
            }
            callPlayer = null;
        }
    }

    private void notifyJs(final String msg) {
        ui.post(
                () -> {
                    if (wv == null) return;
                    wv.evaluateJavascript(
                            "window.w4OnCallAudio && window.w4OnCallAudio(" + quote(msg) + ")", null);
                });
    }

    private static String quote(String s) {
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'";
    }

    private void playRaw(final int resId) {
        ui.post(
                () -> {
                    try {
                        MediaPlayer mp = MediaPlayer.create(this, resId);
                        if (mp == null) return;
                        mp.setAudioAttributes(
                                new AudioAttributes.Builder()
                                        .setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
                                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                                        .build());
                        mp.setVolume(1.0f, 1.0f);
                        mp.setOnCompletionListener(MediaPlayer::release);
                        mp.start();
                    } catch (Throwable ignored) {
                        // 播放失败静默忽略
                    }
                });
    }

    @Override
    public void onBackPressed() {
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        if (webView == null) {
            super.onBackPressed();
            return;
        }
        String js =
                "(function(){"
                        + "if (window.w4HandleBack && window.w4HandleBack() === 'handled') return 'handled';"
                        + "if (window.history.length > 1) { window.history.back(); return 'handled'; }"
                        + "return 'unhandled';"
                        + "})()";
        webView.evaluateJavascript(
                js,
                value -> {
                    if (value != null && value.contains("handled")) return;
                    moveTaskToBack(true);
                });
    }
}
