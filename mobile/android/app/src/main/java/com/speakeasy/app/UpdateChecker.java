package com.speakeasy.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * APK 在线更新。
 *
 * 流程：GET /api/app/latest → 比较 versionCode → 弹窗 → DownloadManager 下载
 *       → 校验 SHA-256 → FileProvider 拉起系统安装器。
 *
 * 说明：Android 不允许侧载应用完全静默安装；用户需要在系统安装器里点「安装」。
 * 8.0+ 需要「安装未知应用」权限，会引导用户去系统设置打开，回来后自动继续安装。
 */
public final class UpdateChecker {
    private static final String TAG = "SpeakEasyUpdate";
    private static final String BASE = "https://47-104-24-236.sslip.io";
    private static final String CHANNEL = "release";

    private static boolean checked = false;
    private static long downloadId = -1L;
    private static File pendingApk = null;
    private static boolean receiverRegistered = false;

    private UpdateChecker() {}

    public static void checkAndPrompt(final Activity activity) {
        if (checked) return;
        checked = true;
        new Thread(
                        () -> {
                            try {
                                JSONObject info = fetchLatest();
                                if (info == null || !info.optBoolean("available", false)) return;
                                final int latest = info.optInt("versionCode", 0);
                                final int current = BuildConfig.VERSION_CODE;
                                if (latest <= current) return;
                                final String versionName = info.optString("versionName", "");
                                final String changelog = info.optString("changelog", "");
                                final String apkUrl = info.optString("apkUrl", "");
                                final String sha256 = info.optString("sha256", "");
                                final boolean force = info.optBoolean("force", false);
                                final int minVersion = info.optInt("minVersionCode", 0);
                                final boolean mandatory = force || current < minVersion;
                                if (apkUrl.isEmpty()) return;
                                final String absoluteUrl =
                                        apkUrl.startsWith("http") ? apkUrl : BASE + apkUrl;
                                Log.i(TAG, "发现新版本 " + latest + "（当前 " + current + "）");
                                activity.runOnUiThread(
                                        () ->
                                                showDialog(
                                                        activity,
                                                        versionName,
                                                        changelog,
                                                        absoluteUrl,
                                                        sha256,
                                                        latest,
                                                        mandatory));
                            } catch (Throwable error) {
                                Log.w(TAG, "检查更新失败: " + error);
                            }
                        })
                .start();
    }

    private static JSONObject fetchLatest() throws Exception {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(BASE + "/api/app/latest?platform=android&channel=" + CHANNEL);
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(6000);
            conn.setReadTimeout(8000);
            conn.setRequestMethod("GET");
            conn.setRequestProperty("Accept", "application/json");
            if (conn.getResponseCode() != 200) return null;
            try (InputStream in = conn.getInputStream()) {
                byte[] buffer = new byte[8192];
                int read;
                StringBuilder sb = new StringBuilder();
                while ((read = in.read(buffer)) > 0) {
                    sb.append(new String(buffer, 0, read, StandardCharsets.UTF_8));
                }
                return new JSONObject(sb.toString());
            }
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static void showDialog(
            final Activity activity,
            String versionName,
            String changelog,
            final String apkUrl,
            final String sha256,
            final int latestCode,
            final boolean mandatory) {
        try {
            StringBuilder message = new StringBuilder();
            if (changelog != null && !changelog.trim().isEmpty()) {
                message.append(changelog.trim());
                message.append("\n\n");
            }
            message.append(mandatory ? "需要更新后才能继续使用。" : "建议更新到最新版本。");

            AlertDialog.Builder builder =
                    new AlertDialog.Builder(activity)
                            .setTitle("新版本 " + versionName)
                            .setMessage(message.toString())
                            .setPositiveButton(
                                    "立即更新",
                                    (dialog, which) ->
                                            startDownload(
                                                    activity, apkUrl, sha256, latestCode));
            if (!mandatory) {
                builder.setNegativeButton("稍后", (dialog, which) -> {});
            }
            builder.setCancelable(!mandatory);
            builder.show();
        } catch (Throwable error) {
            Log.w(TAG, "弹窗失败: " + error);
        }
    }

    private static void startDownload(
            Activity activity, String url, String sha256, int versionCode) {
        try {
            File dir = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
            if (dir == null) dir = activity.getCacheDir();
            if (!dir.exists()) dir.mkdirs();
            final File target = new File(dir, "speakeasy-update-" + versionCode + ".apk");
            if (target.exists()) target.delete();
            pendingApk = target;
            pendingSha256 = sha256;

            DownloadManager manager =
                    (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) {
                Toast.makeText(activity, "系统下载服务不可用", Toast.LENGTH_SHORT).show();
                return;
            }
            DownloadManager.Request request =
                    new DownloadManager.Request(Uri.parse(url))
                            .setTitle("SpeakEasy 更新")
                            .setDescription("正在下载新版本…")
                            .setMimeType("application/vnd.android.package-archive")
                            .setNotificationVisibility(
                                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                            .setDestinationUri(Uri.fromFile(target));
            downloadId = manager.enqueue(request);
            registerReceiver(activity);
            Toast.makeText(activity, "正在下载更新…", Toast.LENGTH_SHORT).show();
        } catch (Throwable error) {
            Log.w(TAG, "下载启动失败: " + error);
            Toast.makeText(activity, "下载失败：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG)
                    .show();
        }
    }

    private static String pendingSha256 = "";

    private static void registerReceiver(Activity activity) {
        if (receiverRegistered) return;
        IntentFilter filter = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            activity.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED);
        } else {
            activity.registerReceiver(receiver, filter);
        }
        receiverRegistered = true;
    }

    private static final BroadcastReceiver receiver =
            new BroadcastReceiver() {
                @Override
                public void onReceive(Context context, Intent intent) {
                    if (!DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(intent.getAction())) return;
                    long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
                    if (id != downloadId || pendingApk == null) return;
                    File apk = pendingApk;
                    String expected = pendingSha256;
                    if (!apk.exists() || apk.length() == 0) {
                        toastAsync(context, "更新包下载失败，请重试");
                        return;
                    }
                    if (expected != null && !expected.isEmpty()) {
                        String actual = sha256(apk);
                        if (!expected.equalsIgnoreCase(actual)) {
                            apk.delete();
                            toastAsync(context, "更新包校验失败（可能下载不完整），请重试");
                            return;
                        }
                    }
                    pendingApk = null;
                    Activity activity = context instanceof Activity ? (Activity) context : null;
                    if (activity != null) {
                        install(activity, apk);
                    } else {
                        installFromContext(context, apk);
                    }
                }
            };

    private static void toastAsync(Context context, String message) {
        Log.w(TAG, message);
        Toast.makeText(context.getApplicationContext(), message, Toast.LENGTH_LONG).show();
    }

    /** 权限引导后回到 App 时继续安装。 */
    public static void onResume(Activity activity) {
        File apk = pendingApk;
        if (apk == null || !apk.exists()) return;
        if (canInstall(activity)) {
            pendingApk = null;
            install(activity, apk);
        }
    }

    private static boolean canInstall(Activity activity) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            return activity.getPackageManager().canRequestPackageInstalls();
        }
        return true;
    }

    private static void install(Activity activity, File apk) {
        if (!canInstall(activity)) {
            pendingApk = apk;
            Toast.makeText(activity, "请允许「安装未知应用」后再返回，会自动继续安装", Toast.LENGTH_LONG)
                    .show();
            try {
                Intent intent =
                        new Intent(
                                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                        Uri.parse("package:" + activity.getPackageName()))
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                activity.startActivity(intent);
            } catch (Throwable error) {
                Log.w(TAG, "打开安装权限设置失败: " + error);
            }
            return;
        }
        try {
            Uri uri =
                    FileProvider.getUriForFile(
                            activity, activity.getPackageName() + ".fileprovider", apk);
            Intent intent =
                    new Intent(Intent.ACTION_VIEW)
                            .setDataAndType(
                                    uri, "application/vnd.android.package-archive")
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            activity.startActivity(intent);
        } catch (Throwable error) {
            Log.w(TAG, "拉起安装器失败: " + error);
            Toast.makeText(activity, "无法打开安装器：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG)
                    .show();
        }
    }

    private static void installFromContext(Context context, File apk) {
        try {
            Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".fileprovider", apk);
            Intent intent =
                    new Intent(Intent.ACTION_VIEW)
                            .setDataAndType(uri, "application/vnd.android.package-archive")
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(intent);
        } catch (Throwable error) {
            Log.w(TAG, "安装失败: " + error);
        }
    }

    private static String sha256(File file) {
        try (FileInputStream in = new FileInputStream(file)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[8192];
            int read;
            while ((read = in.read(buffer)) > 0) {
                digest.update(buffer, 0, read);
            }
            byte[] hash = digest.digest();
            StringBuilder sb = new StringBuilder();
            for (byte b : hash) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (Throwable error) {
            Log.w(TAG, "计算 sha256 失败: " + error);
            return "";
        }
    }
}
