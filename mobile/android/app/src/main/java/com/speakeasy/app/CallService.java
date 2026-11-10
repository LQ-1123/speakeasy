package com.speakeasy.app;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.media.*;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;
import android.os.*;
import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import java.util.*;
import java.util.concurrent.*;
import okhttp3.*;
import okio.ByteString;
import org.json.JSONObject;

/** Foreground service owns transport, recording, routing and playback even with no Activity attached. */
public final class CallService extends Service {
    static final String ACTION_START = "com.speakeasy.app.call.START";
    static final String ACTION_CONTROL = "com.speakeasy.app.call.CONTROL";
    static final String ACTION_STOP = "com.speakeasy.app.call.STOP";
    private static final String CHANNEL = "ongoing_call";
    private static volatile Json state = new Json().put("state", "idle");
    static Json snapshot() { return state; }
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final OkHttpClient client = new OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
            .connectTimeout(5, TimeUnit.SECONDS).readTimeout(8, TimeUnit.SECONDS).build();
    private final Deque<JSONObject> playback = new ArrayDeque<>();
    private final Set<String> seenAudio = new HashSet<>();
    private final Set<String> invalidTurns = new HashSet<>();
    private final NativePolicy.Vad vad = new NativePolicy.Vad();
    private final NativePolicy.ReplyGate replyGate = new NativePolicy.ReplyGate();
    private boolean turnFinished;
    private volatile boolean running, connected, muted, interruptedBySystem, outputActive;
    private boolean reconnecting, speaker, stopping, downloading;
    private long reconnectDeadline, sequence;
    private int generation, connectionGeneration;
    private String turnId = "", callId = "", origin = "", socketPath = "", friendName = "", friendId = "";
    private JSONObject identity;
    private WebSocket socket;
    private AudioRecord recorder;
    private AcousticEchoCanceler aec;
    private NoiseSuppressor ns;
    private MediaPlayer player;
    private java.io.File currentAudioFile;
    private AudioManager audio;
    private AudioFocusRequest focus;
    private AudioManager.OnAudioFocusChangeListener focusListener;
    private PowerManager.WakeLock wake;
    private BroadcastReceiver routes;
    private int priorAudioMode;
    private boolean priorSpeaker;
    private boolean priorSco;
    private AudioDeviceCallback devices;

    @Override public void onCreate() {
        super.onCreate();
        audio = (AudioManager) getSystemService(AUDIO_SERVICE);
        devices = new AudioDeviceCallback() {
            @Override public void onAudioDevicesAdded(AudioDeviceInfo[] changed) { if (running) route(); }
            @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] changed) { if (running) route(); }
        };
        audio.registerAudioDeviceCallback(devices, ui);
        routes = new BroadcastReceiver() { @Override public void onReceive(Context context, Intent intent) {
            if (AudioManager.ACTION_AUDIO_BECOMING_NOISY.equals(intent.getAction())) speaker = false;
            if (running) route();
            publish(state.optString("state", "connected"));
        }};
        IntentFilter filter = new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY);
        filter.addAction(Intent.ACTION_HEADSET_PLUG);
        androidx.core.content.ContextCompat.registerReceiver(this, routes, filter, androidx.core.content.ContextCompat.RECEIVER_NOT_EXPORTED);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        if (ACTION_STOP.equals(intent.getAction())) { end(true); return START_NOT_STICKY; }
        if (ACTION_CONTROL.equals(intent.getAction())) { control(intent.getStringExtra("json")); return START_NOT_STICKY; }
        if (!ACTION_START.equals(intent.getAction())) return START_NOT_STICKY;
        try {
            JSONObject config = new JSONObject(intent.getStringExtra("json"));
            if (running) {
                if (callId.equals(config.optString("callId"))) { publish(state.optString("state")); return START_NOT_STICKY; }
                throw new IllegalStateException("Another call is already active");
            }
            callId = config.getString("callId"); origin = config.getString("apiOrigin"); socketPath = config.getString("wsPath"); friendName = config.optString("friendName", "Sonder"); friendId = config.optString("friendId");
            if (!origin.equals(BuildConfig.API_BASE)) throw new SecurityException("Unconfigured API origin");
            NativePolicy.socketUrl(origin, socketPath, callId);
            identity = new NativeIdentityStore(this).read();
            if (identity == null) throw new SecurityException("Saved identity unavailable");
            startNotification();
            running = true; stopping = false; muted = false; interruptedBySystem = false; generation++; connectionGeneration++;
            priorAudioMode = audio.getMode(); priorSpeaker = audio.isSpeakerphoneOn(); priorSco = audio.isBluetoothScoOn();
            requestFocus(); audio.setMode(AudioManager.MODE_IN_COMMUNICATION); route();
            wake = ((PowerManager) getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SpeakEasy:call");
            wake.acquire();
            startRecording(); publish("connecting"); connect(config.getString("ticket"));
        } catch (Exception failure) { error("call_start", failure.getMessage(), false); end(false); }
        // OS process death ends the call; do not resurrect a mic without a user-visible Activity start.
        return START_NOT_STICKY;
    }

    private void startNotification() {
        NotificationManager notifications = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26) notifications.createNotificationChannel(new NotificationChannel(CHANNEL, "Ongoing calls", NotificationManager.IMPORTANCE_LOW));
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent content = PendingIntent.getActivity(this, 31, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 32, new Intent(this, CallService.class).setAction(ACTION_STOP), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new NotificationCompat.Builder(this, CHANNEL).setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(friendName).setContentText("Sonder call in progress").setOngoing(true).setOnlyAlertOnce(true)
                .setCategory(NotificationCompat.CATEGORY_CALL).setContentIntent(content).addAction(0, "End call", stop).build();
        ServiceCompat.startForeground(this, 31, notification, Build.VERSION.SDK_INT >= 30 ? ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE | ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK : 0);
    }

    private void connect(String ticket) {
        final int epoch = connectionGeneration;
        WebSocket next = client.newWebSocket(new Request.Builder().url(NativePolicy.socketUrl(origin, socketPath, callId)).build(), new WebSocketListener() {
            @Override public void onOpen(WebSocket ws, Response response) {
                if (!running || epoch != connectionGeneration) { ws.close(1000, "stale"); return; }
                Json auth = new Json().put("type", "auth").put("ticket", ticket);
                if (sequence > 0) auth.put("last_sequence", sequence);
                ws.send(auth.toString());
            }
            @Override public void onMessage(WebSocket ws, String text) { ui.post(() -> { if (running && epoch == connectionGeneration && ws == socket) event(text); }); }
            @Override public void onFailure(WebSocket ws, Throwable failure, Response response) { ui.post(() -> { if (running && epoch == connectionGeneration && ws == socket) disconnected(failure.getMessage()); }); }
            @Override public void onClosing(WebSocket ws, int code, String reason) { ws.close(code, reason); ui.post(() -> { if (running && epoch == connectionGeneration && ws == socket) disconnected(reason); }); }
            @Override public void onClosed(WebSocket ws, int code, String reason) { ui.post(() -> { if (running && epoch == connectionGeneration && ws == socket) disconnected(reason); }); }
        });
        socket = next;
        ui.postDelayed(() -> { if (running && !connected && epoch == connectionGeneration && next == socket) disconnected("Call connection timed out"); }, 12_000);
    }

    private final Runnable ping = new Runnable() { @Override public void run() {
        if (!running) return; if (connected) send(new Json().put("type", "ping")); ui.postDelayed(this, 10_000);
    }};
    private void event(String raw) {
        try {
            JSONObject event = new JSONObject(raw);
            long next = event.optLong("sequence", 0);
            if (next > 0 && next <= sequence) return;
            if (next > 0) sequence = next;
            String type = event.optString("type");
            if ("ready".equals(type)) {
                connected = true; reconnecting = false; reconnectDeadline = 0; vad.reset(); replyGate.finished(); turnFinished = false;
                send(new Json().put("type", "mute").put("muted", muted));
                ui.removeCallbacks(ping); ui.post(ping); publish(interruptedBySystem ? "paused" : "connected");
            }
            String eventTurn = event.optString("turn_id");
            if (!eventTurn.isEmpty() && invalidTurns.contains(eventTurn) && !"interrupted".equals(type)) return;
            if (("responding".equals(type) || "transcript".equals(type)) && !eventTurn.isEmpty()) turnId = eventTurn;
            if ("responding".equals(type)) { replyGate.utteranceSent(); turnFinished = false; }
            if ("audio".equals(type) && connected) {
                String id = event.optString("audio_id");
                if (!seenAudio.contains(id) && !invalidTurns.contains(event.optString("turn_id"))) { seenAudio.add(id); playback.add(event); if (player == null) nextAudio(); }
            }
            if ("turn_done".equals(type)) { turnFinished = true; if (player == null && !downloading && playback.isEmpty()) replyGate.finished(); }
            if ("interrupted".equals(type)) { replyGate.finished(); stopPlayback(); }
            if ("error".equals(type)) replyGate.finished();
            MainActivity.dispatchNativeEvent(this, "event", event);
            if ("ended".equals(type)) end(false);
            if ("error".equals(type) && !event.optBoolean("recoverable")) end(false);
        } catch (Exception failure) { error("protocol", "Invalid call event", true); }
    }

    private void disconnected(String reason) {
        if (!running || reconnecting) return;
        connected = false; reconnecting = true; stopPlayback(); vad.reset();
        if (socket != null) socket.cancel(); socket = null;
        connectionGeneration++; if (reconnectDeadline == 0) reconnectDeadline = SystemClock.elapsedRealtime() + 20_000; publish("reconnecting");
        io.execute(() -> {
            while (running && reconnecting && SystemClock.elapsedRealtime() < reconnectDeadline) {
                try {
                    try (Response response = client.newCall(auth(origin + "/api/v3/calls/" + callId + "/ticket").post(RequestBody.create(new byte[0])).build()).execute()) {
                        if (response.code() == 401 || response.code() == 403 || response.code() == 404 || response.code() == 410) { ui.post(() -> { error("session_expired", "Call session is no longer available", false); end(false); }); return; }
                        if (!response.isSuccessful() || response.body() == null) throw new IllegalStateException("Ticket request failed");
                        JSONObject fresh = new JSONObject(response.body().string());
                        String ticket = fresh.getString("ticket"), path = fresh.getString("ws_path");
                        NativePolicy.socketUrl(origin, path, callId);
                        ui.post(() -> {
                            if (!running) return; socketPath = path; reconnecting = false; connect(ticket);
                            ui.postDelayed(() -> { if (running && !connected) disconnected("Connection not ready"); }, 5_000);
                        }); return;
                    }
                } catch (Exception failure) { try { Thread.sleep(1_000); } catch (InterruptedException ignored) { return; } }
            }
            ui.post(() -> { if (running && !connected) { error("reconnect_timeout", "Could not reconnect the call within 20 seconds", false); end(false); } });
        });
    }

    private Request.Builder auth(String url) throws Exception {
        JSONObject saved = new NativeIdentityStore(this).read();
        if (saved != null && saved.optString("user_id").equals(identity.optString("user_id"))) identity = saved;
        return new Request.Builder().url(url).header("X-User-Id", identity.getString("user_id")).header("X-User-Token", identity.getString("token"));
    }

    private void nextAudio() {
        if (!running || !connected || interruptedBySystem || player != null || downloading) return;
        JSONObject event = playback.poll(); if (event == null) { if (turnFinished) replyGate.finished(); return; }
        try {
            String url = java.net.URI.create(origin + "/").resolve(event.getString("audio_url")).toString();
            if (!NativePolicy.trustedAudio(origin, url)) throw new SecurityException("Untrusted audio host");
            // Download with redirects disabled, then decode local bytes. Auth never follows a redirect to another host.
            int epoch = generation; downloading = true;
            io.execute(() -> {
                java.io.File file = null;
                try (Response response = client.newCall(auth(url).build()).execute()) {
                    if (!response.isSuccessful() || response.body() == null) throw new IllegalStateException("Audio download failed");
                    file = java.io.File.createTempFile("call-", ".audio", getCacheDir());
                    try (java.io.InputStream input = response.body().byteStream(); java.io.OutputStream output = new java.io.FileOutputStream(file)) {
                        byte[] buffer = new byte[8192]; int count; long total = 0;
                        while ((count = input.read(buffer)) != -1) { total += count; if (total > 12_000_000) throw new IllegalStateException("Audio too large"); output.write(buffer, 0, count); }
                    }
                    java.io.File audioFile = file;
                    ui.post(() -> {
                        if (!running || epoch != generation) { audioFile.delete(); return; }
                        downloading = false;
                        try {
                            MediaPlayer mp = new MediaPlayer(); player = mp; currentAudioFile = audioFile;
                            mp.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
                            mp.setDataSource(audioFile.getAbsolutePath());
                            mp.setOnPreparedListener(value -> { if (player == value && running && epoch == generation) { outputActive = true; value.start(); } });
                            mp.setOnCompletionListener(value -> { if (value != player) return; outputActive = false; player = null; currentAudioFile = null; value.release(); audioFile.delete(); ack(event, true); nextAudio(); });
                            mp.setOnErrorListener((value, what, extra) -> { if (value != player) return true; outputActive = false; player = null; currentAudioFile = null; value.release(); audioFile.delete(); playbackFailed(event, "audio_playback", "Could not play call audio"); return true; });
                            mp.prepareAsync();
                        } catch (Exception failure) { audioFile.delete(); playbackFailed(event, "audio_playback", failure.getMessage()); }
                    });
                } catch (Exception failure) {
                    if (file != null) file.delete();
                    ui.post(() -> { if (running && epoch == generation) playbackFailed(event, "audio_download", failure.getMessage()); });
                }
            });
        } catch (Exception failure) { playbackFailed(event, "audio_playback", failure.getMessage()); }
    }

    private void playbackFailed(JSONObject event, String code, String message) {
        String failedTurn = event.optString("turn_id");
        if (!failedTurn.isEmpty()) invalidTurns.add(failedTurn);
        replyGate.finished(); stopPlayback(); ack(event, false); error(code, message, true);
    }

    private void ack(JSONObject event, boolean success) { send(new Json().put("type", success ? "playback_done" : "playback_failed").put("audio_id", event.optString("audio_id")).put("turn_id", event.optString("turn_id"))); }
    private void stopPlayback() {
        playback.clear(); generation++; downloading = false; outputActive = false;
        if (player != null) { try { player.stop(); } catch (Exception ignored) { } player.release(); player = null; }
        if (currentAudioFile != null) { currentAudioFile.delete(); currentAudioFile = null; }
    }

    private void startRecording() {
        int min = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        recorder = new AudioRecord.Builder().setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(16000).setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(Math.max(min, 6400)).build();
        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("Microphone initialization failed");
        if (AcousticEchoCanceler.isAvailable()) { aec = AcousticEchoCanceler.create(recorder.getAudioSessionId()); if (aec != null) aec.setEnabled(true); }
        if (NoiseSuppressor.isAvailable()) { ns = NoiseSuppressor.create(recorder.getAudioSessionId()); if (ns != null) ns.setEnabled(true); }
        AudioRecord capture = recorder; capture.startRecording();
        new Thread(() -> {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO);
            byte[] frame = new byte[640];
            Deque<byte[]> preRoll = new ArrayDeque<>();
            boolean speaking = false;
            while (running) {
                int count; try { count = capture.read(frame, 0, frame.length); } catch (Exception failure) { break; }
                if (count < 0) { ui.post(() -> { error("microphone_lost", "Microphone capture failed", false); end(true); }); break; }
                if (count == 0 || !connected || muted || interruptedBySystem) { vad.reset(); preRoll.clear(); speaking = false; continue; }
                double sum = 0;
                for (int i = 0; i + 1 < count; i += 2) { short sample = (short) ((frame[i] & 255) | (frame[i + 1] << 8)); sum += (double) sample * sample; }
                double rms = Math.sqrt(sum / (count / 2.0));
                // With output playing, require a stronger near-end signal after AEC to avoid echo barge-in.
                int detected = vad.frame(outputActive && rms < 1800 ? 0 : rms, count * 1000 / 32000);
                byte[] bytes = java.util.Arrays.copyOf(frame, count);
                List<byte[]> outgoing = new ArrayList<>();
                if (!speaking) {
                    preRoll.addLast(bytes); while (preRoll.size() > 10) preRoll.removeFirst();
                    if (detected == 1) { speaking = true; outgoing.addAll(preRoll); preRoll.clear(); }
                } else outgoing.add(bytes);
                if (detected == 2) { speaking = false; preRoll.clear(); }
                if (outgoing.isEmpty() && detected != 2) continue;
                ui.post(() -> {
                    if (!running || !connected || muted || interruptedBySystem) return;
                    if (detected == 1) {
                        boolean outstanding = replyGate.speechStarted();
                        if (outstanding || player != null || downloading || !playback.isEmpty()) interrupt();
                    }
                    if (socket != null) for (byte[] packet : outgoing) socket.send(ByteString.of(packet));
                    if (detected == 2) { turnId = UUID.randomUUID().toString(); turnFinished = false; replyGate.utteranceSent(); send(new Json().put("type", "utterance_end").put("turn_id", turnId)); }
                });
            }
        }, "SpeakEasyMicrophone").start();
    }

    private void requestFocus() {
        AudioManager.OnAudioFocusChangeListener listener = change -> ui.post(() -> {
            if (!running) return;
            if (change < 0) { interruptedBySystem = true; interrupt(); publish("paused"); error("audio_interrupted", "Call paused for another audio session", true); }
            else if (change == AudioManager.AUDIOFOCUS_GAIN) { interruptedBySystem = false; vad.reset(); publish("connected"); }
        });
        focusListener = listener;
        int granted;
        if (Build.VERSION.SDK_INT >= 26) {
            focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setOnAudioFocusChangeListener(listener, ui).build();
            granted = audio.requestAudioFocus(focus);
        } else granted = audio.requestAudioFocus(listener, AudioManager.STREAM_VOICE_CALL, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE);
        if (granted != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) throw new IllegalStateException("Audio focus is unavailable");
    }
    private void route() {
        if (Build.VERSION.SDK_INT >= 31) {
            AudioDeviceInfo selected = null;
            for (AudioDeviceInfo device : audio.getAvailableCommunicationDevices()) {
                int type = device.getType();
                if ((type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET) && androidx.core.content.ContextCompat.checkSelfPermission(this, android.Manifest.permission.BLUETOOTH_CONNECT) != android.content.pm.PackageManager.PERMISSION_GRANTED) continue;
                if (speaker && type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) { selected = device; break; }
                if (!speaker && (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET || type == AudioDeviceInfo.TYPE_WIRED_HEADSET || type == AudioDeviceInfo.TYPE_USB_HEADSET)) { selected = device; break; }
                if (!speaker && type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE) selected = device;
            }
            if (selected != null) audio.setCommunicationDevice(selected); else audio.clearCommunicationDevice();
        } else {
            boolean bluetooth = false;
            for (AudioDeviceInfo device : audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) if (device.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) bluetooth = true;
            if (!speaker && bluetooth) { audio.startBluetoothSco(); audio.setBluetoothScoOn(true); }
            else if (!priorSco && audio.isBluetoothScoOn()) { audio.stopBluetoothSco(); audio.setBluetoothScoOn(false); }
            audio.setSpeakerphoneOn(speaker);
        }
    }
    private void control(String raw) {
        if (!running) return;
        try {
            JSONObject action = new JSONObject(raw); String type = action.optString("type");
            if ("end".equals(type)) { end(true); return; }
            if ("mute".equals(type)) { muted = action.optBoolean("muted", !muted); vad.reset(); send(new Json().put("type", "mute").put("muted", muted)); }
            if ("speaker".equals(type)) { speaker = action.optBoolean("speaker", action.optBoolean("enabled", !speaker)); route(); }
            if ("interrupt".equals(type)) interrupt();
            publish(interruptedBySystem ? "paused" : state.optString("state", "connected"));
        } catch (Exception failure) { error("invalid_action", "Invalid call action", true); }
    }
    private void interrupt() { if (!turnId.isEmpty()) invalidTurns.add(turnId); replyGate.finished(); stopPlayback(); send(new Json().put("type", "interrupt").put("turn_id", turnId)); }
    private void send(JSONObject value) { if (socket != null) socket.send(value.toString()); }
    private void publish(String status) {
        String phase = "connected".equals(status) ? "listening" : "paused".equals(status) ? "reconnecting" : "failed".equals(status) ? "error" : status;
        state = new Json().put("state", status).put("phase", phase).put("call_id", callId).put("friend_name", friendName).put("friend_id", friendId).put("friendId", friendId).put("callId", callId).put("friendName", friendName).put("muted", muted).put("speaker", speaker).put("paused", interruptedBySystem);
        MainActivity.dispatchNativeEvent(this, "call", state);
    }
    private void error(String code, String message, boolean recoverable) { MainActivity.dispatchNativeEvent(this, "event", new Json().put("type", "error").put("code", code).put("message", message).put("recoverable", recoverable)); }
    private void end(boolean notify) {
        if (stopping) return; stopping = true;
        if (notify) send(new Json().put("type", "end"));
        running = false; connected = false; reconnecting = false;
        ui.removeCallbacks(ping); stopPlayback();
        if (socket != null) { socket.close(1000, "ended"); socket = null; }
        if (recorder != null) { try { recorder.stop(); } catch (Exception ignored) { } recorder.release(); recorder = null; }
        if (aec != null) { aec.release(); aec = null; } if (ns != null) { ns.release(); ns = null; }
        if (audio != null) { if (Build.VERSION.SDK_INT >= 31) audio.clearCommunicationDevice(); else if (!priorSco && audio.isBluetoothScoOn()) { audio.stopBluetoothSco(); audio.setBluetoothScoOn(false); } audio.setSpeakerphoneOn(priorSpeaker); audio.setMode(priorAudioMode); if (Build.VERSION.SDK_INT >= 26 && focus != null) audio.abandonAudioFocusRequest(focus); else if (focusListener != null) audio.abandonAudioFocus(focusListener); }
        if (wake != null && wake.isHeld()) wake.release();
        publish("ended"); stopForeground(STOP_FOREGROUND_REMOVE); stopSelf();
    }
    @Override public void onDestroy() { end(false); if (routes != null) unregisterReceiver(routes); if (devices != null) audio.unregisterAudioDeviceCallback(devices); client.dispatcher().cancelAll(); io.shutdownNow(); super.onDestroy(); }
    @Override public IBinder onBind(Intent intent) { return null; }
}
