package com.speakeasy.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.view.Window;
import android.view.WindowManager;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
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
 * 发现更新后先在后台预下载，面板显示进度；完成校验后按钮变为「立即安装」，
 * 点击直接拉起系统安装器，不再让用户对着按钮等待下载。
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
    private static int downloadPercent = 0;
    private static boolean downloadReady = false;
    private static File pendingApk = null;
    private static String pendingSha256 = "";
    private static boolean receiverRegistered = false;
    private static boolean installRequested = false;
    private static Handler progressHandler;
    private static Runnable progressTick;
    private static long lastDownloadBytes = -1L;
    private static long lastDownloadMovement = 0L;
    private static String downloadUrl = "";
    private static String downloadSha256 = "";
    private static int downloadVersionCode = 0;
    private static String pendingUrl = "";
    private static int pendingVersionCode = 0;

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
            String message = changelog == null ? "" : changelog.trim();
            if (!message.isEmpty()) message += "\n\n";
            message += mandatory ? "需要更新后才能继续使用。" : "建议更新到最新版本。";

            boolean shown =
                    UpdateSheet.show(
                            activity,
                            versionName,
                            message,
                            mandatory,
                            () -> onPrimary(activity, apkUrl, sha256, latestCode));
            if (shown) {
                // 打开面板时就开始预下载；下载完成前不会自动拉起安装器。
                startDownload(activity, apkUrl, sha256, latestCode);
            } else {
                AlertDialog.Builder builder =
                        new AlertDialog.Builder(activity)
                                .setTitle("新版本 " + versionName)
                                .setMessage(message)
                                .setPositiveButton(
                                        "立即更新",
                                        (dialog, which) ->
                                                onPrimary(activity, apkUrl, sha256, latestCode));
                if (!mandatory) {
                    builder.setNegativeButton("稍后", (dialog, which) -> {});
                }
                builder.setCancelable(!mandatory);
                builder.show();
            }
        } catch (Throwable error) {
            Log.w(TAG, "弹窗失败: " + error);
        }
    }

    /** 用户点击「立即更新 / 立即安装」：已就绪直接安装，下载中则继续等待。 */
    private static void onPrimary(
            Activity activity, String url, String sha256, int versionCode) {
        if (downloadReady && pendingApk != null && pendingApk.exists()) {
            install(activity, pendingApk);
            return;
        }
        if (downloadId >= 0L) {
            UpdateSheet.progress(activity, downloadPercent, true);
            return;
        }
        startDownload(activity, url, sha256, versionCode);
    }

    private static void startDownload(
            Activity activity, String url, String sha256, int versionCode) {
        try {
            if (downloadId >= 0L) {
                UpdateSheet.progress(activity, downloadPercent, true);
                return;
            }

            File dir = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
            if (dir == null) dir = activity.getCacheDir();
            if (dir == null) {
                UpdateSheet.failed(activity);
                toastAsync(activity, "无法准备更新目录");
                return;
            }
            if (!dir.exists()) dir.mkdirs();

            File target = new File(dir, "speakeasy-update-" + versionCode + ".apk");
            if (target.exists() && target.length() > 0) {
                if (sha256Matches(target, sha256)) {
                    pendingApk = target;
                    pendingSha256 = sha256;
                    downloadReady = true;
                    UpdateSheet.ready(activity);
                    return;
                }
                target.delete();
            }

            pendingApk = target;
            pendingSha256 = sha256 == null ? "" : sha256;
            downloadReady = false;
            downloadPercent = 0;
            downloadUrl = url;
            downloadSha256 = pendingSha256;
            downloadVersionCode = versionCode;
            lastDownloadBytes = -1L;
            lastDownloadMovement = System.currentTimeMillis();

            DownloadManager manager =
                    (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) {
                startFallbackDownload(activity, url, sha256, versionCode);
                return;
            }
            DownloadManager.Request request =
                    new DownloadManager.Request(Uri.parse(url))
                            .setTitle("Sonder 更新")
                            .setDescription("正在下载新版本…")
                            .setMimeType("application/vnd.android.package-archive")
                            .setNotificationVisibility(
                                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                            .setDestinationUri(Uri.fromFile(target));
            downloadId = manager.enqueue(request);
            registerReceiver(activity);
            UpdateSheet.progress(activity, 0, true);
            startProgressPolling(activity, manager);
        } catch (Throwable error) {
            Log.w(TAG, "下载启动失败: " + error);
            UpdateSheet.failed(activity);
            toastAsync(activity, "下载失败：" + error.getClass().getSimpleName());
        }
    }

    /** 系统下载器无响应时，改用应用内直连下载，避免面板永久停在「下载中」。 */
    private static void restartWithDirectDownload(Activity activity, DownloadManager manager) {
        long id = downloadId;
        downloadId = -1L;
        stopProgressPolling();
        if (id >= 0L && manager != null) {
            try {
                manager.remove(id);
            } catch (Throwable ignored) {
            }
        }
        startFallbackDownload(activity, downloadUrl, downloadSha256, downloadVersionCode);
    }

    private static void startFallbackDownload(
            Activity activity, String url, String sha256, int versionCode) {
        try {
            File dir = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
            if (dir == null) dir = activity.getCacheDir();
            if (dir == null) {
                UpdateSheet.failed(activity);
                return;
            }
            if (!dir.exists()) dir.mkdirs();
            File target = new File(dir, "speakeasy-update-" + versionCode + ".apk");
            pendingApk = target;
            pendingSha256 = sha256 == null ? "" : sha256;
            downloadReady = false;
            downloadPercent = 0;
            UpdateSheet.progress(activity, 0, true);
            new Thread(() -> {
                HttpURLConnection connection = null;
                try {
                    connection = (HttpURLConnection) new URL(url).openConnection();
                    connection.setConnectTimeout(12000);
                    connection.setReadTimeout(25000);
                    connection.setRequestProperty("Accept-Encoding", "identity");
                    int code = connection.getResponseCode();
                    if (code != 200) throw new IllegalStateException("HTTP " + code);
                    int total = connection.getContentLength();
                    try (InputStream in = connection.getInputStream();
                            FileOutputStream out = new FileOutputStream(target)) {
                        byte[] buffer = new byte[16384];
                        long written = 0L;
                        int read;
                        while ((read = in.read(buffer)) > 0) {
                            out.write(buffer, 0, read);
                            written += read;
                            if (total > 0) {
                                downloadPercent = Math.min(99, (int) (written * 100L / total));
                                UpdateSheet.progress(activity, downloadPercent, true);
                            }
                        }
                    }
                    verifyDownload(activity, target, sha256);
                } catch (Throwable error) {
                    Log.w(TAG, "直连下载失败: " + error);
                    if (target.exists()) target.delete();
                    UpdateSheet.failed(activity);
                } finally {
                    if (connection != null) connection.disconnect();
                }
            }).start();
        } catch (Throwable error) {
            Log.w(TAG, "直连下载启动失败: " + error);
            UpdateSheet.failed(activity);
        }
    }

    private static boolean sha256Matches(File file, String expected) {
        if (expected == null || expected.isEmpty()) return true;
        return expected.equalsIgnoreCase(sha256(file));
    }

    private static void startProgressPolling(Activity activity, DownloadManager manager) {
        stopProgressPolling();
        progressHandler = new Handler(Looper.getMainLooper());
        progressTick = new Runnable() {
            @Override public void run() {
                long id = downloadId;
                if (id < 0L) return;
                try {
                    DownloadManager.Query query = new DownloadManager.Query().setFilterById(id);
                    try (Cursor cursor = manager.query(query)) {
                        if (cursor == null || !cursor.moveToFirst()) {
                            scheduleProgressTick();
                            return;
                        }
                        int bytes = cursor.getInt(cursor.getColumnIndexOrThrow(
                                DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                        int total = cursor.getInt(cursor.getColumnIndexOrThrow(
                                DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
                        int status = cursor.getInt(cursor.getColumnIndexOrThrow(
                                DownloadManager.COLUMN_STATUS));
                        long now = System.currentTimeMillis();
                        if (bytes > lastDownloadBytes) {
                            lastDownloadBytes = bytes;
                            lastDownloadMovement = now;
                        }
                        if (status == DownloadManager.STATUS_SUCCESSFUL) {
                            downloadId = -1L;
                            stopProgressPolling();
                            verifyDownload(activity, pendingApk, pendingSha256);
                            return;
                        }
                        if (status == DownloadManager.STATUS_FAILED) {
                            downloadId = -1L;
                            stopProgressPolling();
                            downloadReady = false;
                            UpdateSheet.failed(activity);
                            toastAsync(activity, "更新包下载失败，请重试");
                            return;
                        }
                        downloadPercent = total > 0
                                ? Math.min(99, (int) (bytes * 100L / (long) total))
                                : 0;
                        UpdateSheet.progress(activity, downloadPercent, true);
                        if (lastDownloadBytes <= 0 && now - lastDownloadMovement > 8000L) {
                            restartWithDirectDownload(activity, manager);
                            return;
                        }
                        if (lastDownloadBytes > 0 && now - lastDownloadMovement > 90000L) {
                            restartWithDirectDownload(activity, manager);
                            return;
                        }
                    }
                } catch (Throwable error) {
                    Log.w(TAG, "读取下载进度失败: " + error);
                }
                scheduleProgressTick();
            }
        };
        progressHandler.post(progressTick);
    }

    private static void scheduleProgressTick() {
        if (progressHandler != null && progressTick != null) {
            progressHandler.postDelayed(progressTick, 450L);
        }
    }

    private static void stopProgressPolling() {
        if (progressHandler != null && progressTick != null) {
            progressHandler.removeCallbacks(progressTick);
        }
        progressHandler = null;
        progressTick = null;
    }

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
                    stopProgressPolling();
                    downloadId = -1L;
                    verifyDownload(context, pendingApk, pendingSha256);
                }
            };

    /** SHA-256 在后台线程计算，避免 15MB 安装包阻塞主线程。 */
    private static void verifyDownload(Context context, File apk, String expected) {
        new Thread(() -> {
            if (apk == null || !apk.exists() || apk.length() == 0) {
                downloadReady = false;
                UpdateSheet.failed(context);
                toastAsync(context, "更新包下载失败，请重试");
                return;
            }
            if (expected != null && !expected.isEmpty() && !sha256Matches(apk, expected)) {
                apk.delete();
                downloadReady = false;
                UpdateSheet.failed(context);
                toastAsync(context, "更新包校验失败（可能下载不完整），请重试");
                return;
            }
            pendingApk = apk;
            downloadPercent = 100;
            downloadReady = true;
            UpdateSheet.ready(context);
        }).start();
    }

    private static void toastAsync(Context context, String message) {
        Log.w(TAG, message);
        Toast.makeText(context.getApplicationContext(), message, Toast.LENGTH_LONG).show();
    }

    /** 从「安装未知应用」设置返回后继续安装；普通回到前台只刷新面板。 */
    public static void onResume(Activity activity) {
        if (!downloadReady || pendingApk == null || !pendingApk.exists()) return;
        if (installRequested && canInstall(activity)) {
            installRequested = false;
            install(activity, pendingApk);
            return;
        }
        UpdateSheet.ready(activity);
    }

    private static boolean canInstall(Activity activity) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            return activity.getPackageManager().canRequestPackageInstalls();
        }
        return true;
    }

    private static void install(Activity activity, File apk) {
        if (apk == null || !apk.exists()) {
            UpdateSheet.failed(activity);
            return;
        }
        UpdateSheet.installing(activity);
        if (!canInstall(activity)) {
            pendingApk = apk;
            installRequested = true;
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
            installRequested = false;
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
            UpdateSheet.ready(activity);
            Toast.makeText(activity, "无法打开安装器：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG)
                    .show();
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
