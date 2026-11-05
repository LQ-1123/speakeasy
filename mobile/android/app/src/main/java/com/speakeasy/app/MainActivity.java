package com.speakeasy.app;

import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            WebView webView = getBridge() != null ? getBridge().getWebView() : null;
            if (webView != null) {
                WebSettings s = webView.getSettings();
                s.setMediaPlaybackRequiresUserGesture(false);
                s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

                // 原生提示音：播放内置铃声（比 ToneGenerator 好听、也更响）
                webView.addJavascriptInterface(new Object() {
                    @JavascriptInterface
                    public void ding() {
                        playRaw(R.raw.notify_message);
                    }

                    @JavascriptInterface
                    public void dong() {
                        playRaw(R.raw.notify_request);
                    }

                    /** 原生播放在线音频（通话语音用；绕开 WebView 的音频限制） */
                    @JavascriptInterface
                    public void playUrl(final String url) {
                        if (url == null) return;
                        if (!url.startsWith("http://") && !url.startsWith("https://")) return;
                        ui.post(
                                () -> {
                                    try {
                                        MediaPlayer mp = new MediaPlayer();
                                        mp.setAudioAttributes(
                                                new AudioAttributes.Builder()
                                                        .setUsage(AudioAttributes.USAGE_MEDIA)
                                                        .setContentType(
                                                                AudioAttributes
                                                                        .CONTENT_TYPE_SPEECH)
                                                        .build());
                                        mp.setDataSource(url);
                                        mp.setVolume(1.0f, 1.0f);
                                        mp.setOnPreparedListener(MediaPlayer::start);
                                        mp.setOnCompletionListener(MediaPlayer::release);
                                        mp.setOnErrorListener(
                                                (m, what, extra) -> {
                                                    m.release();
                                                    return true;
                                                });
                                        mp.prepareAsync();
                                    } catch (Throwable ignored) {
                                        // 播放失败静默忽略
                                    }
                                });
                    }
                }, "AndroidTone");
            }
        } catch (Throwable ignored) {
            // 设置失败不影响 App 启动
        }
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
