package com.speakeasy.app;

import android.app.Activity;
import android.app.Dialog;
import android.content.Context;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.os.Build;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.TextView;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

/**
 * 品牌化 App 更新面板。
 *
 * 用自绘的 bottom sheet 取代系统 AlertDialog 的灰白默认样式；下载进度、
 * 校验状态和安装器跳转都在同一张卡片里承接，避免用户误以为卡住。
 */
final class UpdateSheet {
    private static Dialog current;
    private static TextView versionView;
    private static TextView changelogView;
    private static TextView headlineView;
    private static TextView badgeView;
    private static View progressGroup;
    private static TextView progressLabel;
    private static TextView progressPercent;
    private static ProgressBar progressBar;
    private static Button primary;
    private static Button secondary;
    private static boolean mandatory;
    private static Runnable onInstall;

    private UpdateSheet() {}

    static boolean show(
            Activity activity,
            String versionName,
            String changelog,
            boolean mustUpdate,
            Runnable installAction) {
        try {
            dismiss();
            mandatory = mustUpdate;
            onInstall = installAction;
            Dialog dialog = new Dialog(activity);
            dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
            int layoutId =
                    activity.getResources()
                            .getIdentifier("dialog_app_update", "layout", activity.getPackageName());
            if (layoutId == 0) return false;
            View view = LayoutInflater.from(activity).inflate(layoutId, null);
            dialog.setContentView(view);
            dialog.setCanceledOnTouchOutside(!mustUpdate);
            dialog.setCancelable(!mustUpdate);
            Window window = dialog.getWindow();
            if (window != null) {
                window.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
                window.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
                window.setDimAmount(0.55f);
                window.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    window.setDecorFitsSystemWindows(false);
                }
                window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
            }
            versionView = view.findViewById(id(activity, "update_version"));
            changelogView = view.findViewById(id(activity, "update_changelog"));
            headlineView = view.findViewById(id(activity, "update_headline"));
            badgeView = view.findViewById(id(activity, "update_mandatory_badge"));
            progressGroup = view.findViewById(id(activity, "update_progress_group"));
            progressLabel = view.findViewById(id(activity, "update_progress_label"));
            progressPercent = view.findViewById(id(activity, "update_progress_percent"));
            progressBar = view.findViewById(id(activity, "update_progress"));
            primary = view.findViewById(id(activity, "update_primary"));
            secondary = view.findViewById(id(activity, "update_secondary"));

            versionView.setText("v" + safe(versionName) + "  \u00b7  安全更新");
            changelogView.setText(readableChangelog(changelog));
            headlineView.setText(mustUpdate ? "这一版需要更新后才能继续。" : "更顺滑的一版，已经准备好了。");
            badgeView.setVisibility(mustUpdate ? View.VISIBLE : View.GONE);
            secondary.setVisibility(mustUpdate ? View.GONE : View.VISIBLE);

            primary.setOnClickListener(v -> {
                setBusy(true);
                if (onInstall != null) onInstall.run();
            });
            secondary.setOnClickListener(v -> dismiss());
            view.setOnClickListener(v -> {
                if (!mandatory) dismiss();
            });
            View sheet = view.findViewById(id(activity, "update_sheet"));
            if (sheet != null) {
                sheet.setOnClickListener(v -> { /* 面板内点击不关闭 */ });
                applySystemBarInsets(sheet);
            }

            dialog.show();
            current = dialog;
            view.setAlpha(0f);
            view.animate().alpha(1f).setDuration(180L).start();
            if (sheet != null) {
                sheet.setTranslationY(dp(activity, 40));
                sheet.animate().translationY(0f).setDuration(260L).start();
            }
            return true;
        } catch (Throwable error) {
            return false;
        }
    }

    static void progress(Context context, int percent, boolean downloading) {
        Activity host = currentActivity(context);
        if (host == null) return;
        host.runOnUiThread(() -> {
            if (progressGroup == null) return;
            progressGroup.setVisibility(View.VISIBLE);
            if (progressLabel != null) {
                progressLabel.setText(downloading ? "正在下载更新" : "下载完成，正在校验");
            }
            if (progressPercent != null) {
                progressPercent.setText(Math.max(0, Math.min(100, percent)) + "%");
            }
            if (progressBar != null) {
                progressBar.setProgress(Math.max(0, Math.min(100, percent)));
            }
            if (primary != null) {
                primary.setEnabled(false);
                primary.setAlpha(0.72f);
                primary.setText(downloading ? "下载中\u2026" : "准备安装\u2026");
            }
            if (secondary != null && !mandatory) {
                secondary.setEnabled(!downloading);
                secondary.setAlpha(downloading ? 0.55f : 1f);
            }
        });
    }
    static void ready(Context context) {
        Activity host = currentActivity(context);
        if (host == null) return;
        host.runOnUiThread(() -> {
            if (progressGroup != null) progressGroup.setVisibility(View.VISIBLE);
            if (progressLabel != null) progressLabel.setText("安装包已准备好");
            if (progressPercent != null) progressPercent.setText("100%");
            if (progressBar != null) progressBar.setProgress(100);
            if (primary != null) {
                primary.setEnabled(true);
                primary.setAlpha(1f);
                primary.setText("立即安装");
                primary.setOnClickListener(v -> {
                    setBusy(true);
                    if (onInstall != null) onInstall.run();
                });
            }
            if (secondary != null && !mandatory) {
                secondary.setEnabled(true);
                secondary.setAlpha(1f);
            }
        });
    }

    static void failed(Context context) {
        Activity host = currentActivity(context);
        if (host == null) return;
        host.runOnUiThread(() -> {
            if (progressGroup != null) progressGroup.setVisibility(View.GONE);
            if (primary != null) {
                primary.setEnabled(true);
                primary.setAlpha(1f);
                primary.setText("重新下载");
                primary.setOnClickListener(v -> {
                    if (onInstall != null) onInstall.run();
                });
            }
        });
    }

    static void installing(Context context) {
        Activity host = currentActivity(context);
        if (host == null) return;
        host.runOnUiThread(() -> {
            if (progressLabel != null) progressLabel.setText("安装包已验证");
            if (primary != null) {
                primary.setEnabled(false);
                primary.setAlpha(0.72f);
                primary.setText("正在打开安装器\u2026");
            }
        });
    }

    static void dismiss() {
        Dialog dialog = current;
        current = null;
        progressGroup = null;
        if (dialog != null) {
            try { dialog.dismiss(); } catch (Throwable ignored) {}
        }
    }

    private static void setBusy(boolean busy) {
        if (primary == null) return;
        primary.setEnabled(!busy);
        primary.setAlpha(busy ? 0.72f : 1f);
        if (secondary != null && !mandatory) {
            secondary.setEnabled(!busy);
            secondary.setAlpha(busy ? 0.55f : 1f);
        }
    }

    private static String readableChangelog(String changelog) {
        String text = safe(changelog);
        if (text.isEmpty()) return "修复了一些体验问题，并优化了聊天与语音的稳定性。";
        return text.replace("\r\n", "\n").replace("\r", "\n").trim();
    }

    private static String safe(String value) {
        return value == null ? "" : value.trim();
    }

    private static Activity currentActivity(Context context) {
        if (context instanceof Activity) return (Activity) context;
        return null;
    }

    private static int id(Activity activity, String name) {
        return activity.getResources().getIdentifier(name, "id", activity.getPackageName());
    }

    private static void applySystemBarInsets(View sheet) {
        final int top = sheet.getPaddingTop();
        final int bottom = sheet.getPaddingBottom();
        ViewCompat.setOnApplyWindowInsetsListener(sheet, (view, windowInsets) -> {
            Insets bars = windowInsets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(view.getPaddingLeft(), top + bars.top, view.getPaddingRight(), bottom + bars.bottom);
            return windowInsets;
        });
        ViewCompat.requestApplyInsets(sheet);
    }

    private static int dp(Context context, int value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }
}
