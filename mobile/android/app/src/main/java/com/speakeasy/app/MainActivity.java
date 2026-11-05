package com.speakeasy.app;

import android.media.AudioManager;
import android.media.ToneGenerator;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            WebView webView = getBridge() != null ? getBridge().getWebView() : null;
            if (webView != null) {
                WebSettings s = webView.getSettings();
                s.setMediaPlaybackRequiresUserGesture(false);
                s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

                // 原生提示音：荣耀/华为 WebView 对 WebAudio 支持不稳定，改用系统 ToneGenerator
                final ToneGenerator tg = new ToneGenerator(AudioManager.STREAM_NOTIFICATION, 88);
                final Handler handler = new Handler(Looper.getMainLooper());
                webView.addJavascriptInterface(new Object() {
                    @JavascriptInterface
                    public void ding() {
                        tg.startTone(ToneGenerator.TONE_PROP_BEEP, 140);
                    }

                    @JavascriptInterface
                    public void dong() {
                        tg.startTone(ToneGenerator.TONE_PROP_BEEP, 110);
                        handler.postDelayed(
                                () -> tg.startTone(ToneGenerator.TONE_PROP_ACK, 130), 150);
                    }
                }, "AndroidTone");
            }
        } catch (Throwable ignored) {
            // 设置失败不影响 App 启动
        }
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
