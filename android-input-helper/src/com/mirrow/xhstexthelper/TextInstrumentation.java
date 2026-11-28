package com.mirrow.xhstexthelper;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.graphics.Rect;
import android.net.Uri;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;
import com.mirrow.xhssignals.protocol.SignalProtocol;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Bounded XHS text replacement and authenticated signal-host calls. Never clicks or sends. */
public final class TextInstrumentation extends Instrumentation {
    private static final String TARGET = "com.xingin.xhs";
    private static final int MAX_CHARS = 10000;
    private static final int MAX_BYTES = MAX_CHARS * 4;
    private static final int MAX_BASE64 = ((MAX_BYTES + 2) / 3) * 4;
    private static final int MAX_NODES = 10000;
    private static final Set<String> KEYS = new HashSet<String>(Arrays.asList(
        "op", "left", "top", "right", "bottom", "text_b64", "signal_ids_b64", "full_scan_token"));
    private Bundle arguments;

    @Override public void onCreate(Bundle args) {
        arguments = args == null ? new Bundle() : new Bundle(args);
        start();
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        boolean ok = false;
        String selectedOp = null;
        try {
            for (String key : arguments.keySet()) {
                require(KEYS.contains(key), "invalid_arguments");
            }
            String op = stringArg("op");
            selectedOp = op;
            require("health".equals(op) || "set".equals(op) || "set_empty".equals(op) || "search_favorites".equals(op) || "clear".equals(op) || "notifications".equals(op)
                || "ack".equals(op), "invalid_operation");
            if ("notifications".equals(op) || "ack".equals(op)) {
                Bundle extra = null;
                if ("notifications".equals(op)) {
                    require(arguments.size() == 1, "invalid_arguments");
                } else {
                    require(arguments.size() == (arguments.containsKey("full_scan_token") ? 3 : 2),
                        "invalid_arguments");
                    String ids = stringArg("signal_ids_b64");
                    try { SignalProtocol.decodeIds(ids); }
                    catch (Exception ignored) { throw new Rejected("invalid_signal_ids"); }
                    extra = new Bundle();
                    extra.putString("signal_ids_b64", ids);
                    if (arguments.containsKey("full_scan_token")) {
                        String token = stringArg("full_scan_token");
                        require(token.matches("[a-f0-9]{32}"), "invalid_arguments");
                        extra.putString("full_scan_token", token);
                    }
                }
                Bundle snapshot;
                try {
                    snapshot = getTargetContext().getContentResolver().call(
                        Uri.parse("content://" + SignalProtocol.AUTHORITY), op, null, extra);
                } catch (SecurityException denied) {
                    throw new Rejected("signal_permission_denied");
                } catch (IllegalArgumentException missing) {
                    throw new Rejected("signal_provider_unavailable");
                }
                require(snapshot != null, "signal_provider_unavailable");
                result.putAll(snapshot);
                ok = "ok".equals(snapshot.getString("status"));
                return;
            } else if ("health".equals(op)) {
                require(arguments.size() == 1, "invalid_arguments");
                // Acquiring the automation connection also tests its actual availability.
                require(automation() != null, "automation_unavailable");
            } else {
                boolean clear = "clear".equals(op);
                require(arguments.size() == (clear ? 5 : 6)
                    && (!clear || !arguments.containsKey("text_b64")), "invalid_arguments");
                Rect bounds = new Rect(coordinate("left"), coordinate("top"),
                    coordinate("right"), coordinate("bottom"));
                require(bounds.left < bounds.right && bounds.top < bounds.bottom,
                    "invalid_bounds");
                String text = clear ? "" : decodeText(stringArg("text_b64"));
                result.putString("text_sha256", sha256(text));
                UiAutomation ui = automation();
                replaceText(ui, bounds, text, "set_empty".equals(op));
                if ("search_favorites".equals(op)) submitProfileSearch(ui, bounds, text);
            }
            result.putString("status", "ok");
            ok = true;
        } catch (Rejected failure) {
            result.putString("status", "error");
            result.putString("error", failure.code);
        } catch (Throwable ignored) {
            // Never print an exception: arguments and UI content can occur in messages.
            result.putString("status", "error");
            result.putString("error", "internal_error");
        } finally {
            if (!ok && ("notifications".equals(selectedOp) || "ack".equals(selectedOp))) {
                result.putString("availability", "unknown");
                result.putBoolean("needs_full_scan", true);
            }
            arguments = null;
            finish(ok ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
        }
    }

    private static void submitProfileSearch(UiAutomation ui, Rect bounds, String text) throws Exception {
        AccessibilityNodeInfo root = awaitInitialRoot(ui);
        AccessibilityNodeInfo editor = null;
        try {
            editor = uniqueEditable(root, bounds);
            require(android.os.Build.VERSION.SDK_INT >= 30 && editor.refresh() && eligible(editor, bounds)
                && editor.isFocused() && !editor.isPassword() && !text.isEmpty()
                && "搜索我的笔记/收藏/赞过".contentEquals(String.valueOf(editor.getHintText()))
                && text.contentEquals(String.valueOf(editor.getText())), "profile_search_editor_changed");
            require(editor.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId()),
                "search_editor_action_unavailable");
        } finally {
            if (editor != null) editor.recycle();
            root.recycle();
        }
    }

    private UiAutomation automation() throws Rejected {
        UiAutomation automation = getUiAutomation(
            UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
        require(automation != null, "automation_unavailable");
        AccessibilityServiceInfo info = automation.getServiceInfo();
        require(info != null, "automation_unavailable");
        int requiredFlags = AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
            | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
        info.flags |= requiredFlags;
        automation.setServiceInfo(info);
        AccessibilityServiceInfo configured = automation.getServiceInfo();
        require(configured != null && (configured.flags & requiredFlags) == requiredFlags,
            "automation_configuration_failed");
        return automation;
    }

    private String stringArg(String key) throws Rejected {
        Object value = arguments.get(key);
        require(value instanceof String, "invalid_arguments");
        return (String) value;
    }

    private int coordinate(String key) throws Rejected {
        String value = stringArg(key);
        require(value.matches("[0-9]{1,5}"), "invalid_bounds");
        return Integer.parseInt(value);
    }

    private static String decodeText(String encoded) throws Exception {
        require(encoded.length() <= MAX_BASE64 && encoded.length() % 4 == 0, "invalid_base64");
        int padding = 0;
        for (int i = 0; i < encoded.length(); i++) {
            char ch = encoded.charAt(i);
            if (ch == '=') {
                require(i >= encoded.length() - 2 && ++padding <= 2, "invalid_base64");
            } else {
                require(padding == 0 && ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z')
                    || (ch >= '0' && ch <= '9') || ch == '+' || ch == '/'), "invalid_base64");
            }
        }
        byte[] bytes;
        try { bytes = android.util.Base64.decode(encoded, android.util.Base64.NO_WRAP); }
        catch (IllegalArgumentException ignored) { throw new Rejected("invalid_base64"); }
        require(bytes.length <= MAX_BYTES, "text_too_long");
        require(android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)
            .equals(encoded), "invalid_base64");
        String text;
        try {
            text = StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes)).toString();
        } catch (java.nio.charset.CharacterCodingException ignored) {
            throw new Rejected("invalid_utf8");
        }
        require(text.codePointCount(0, text.length()) <= MAX_CHARS, "text_too_long");
        require(text.indexOf('\0') < 0, "invalid_text");
        return text;
    }

    private static void replaceText(UiAutomation automation, Rect bounds, String text, boolean requireEmpty)
            throws Exception {
        AccessibilityNodeInfo root = awaitInitialRoot(automation);
        AccessibilityNodeInfo target = null;
        try {
            target = uniqueEditable(root, bounds);
            require(target.refresh(), "stale_node");
            require(eligible(target, bounds), "target_changed");
            require(target.isEnabled() && !target.isPassword(), "target_not_writable");
            if (requireEmpty) {
                CharSequence existing = target.getText();
                boolean empty = existing == null || existing.length() == 0;
                if (!empty && android.os.Build.VERSION.SDK_INT >= 26 && target.isShowingHintText()) {
                    CharSequence hint = target.getHintText();
                    empty = hint != null && hint.toString().contentEquals(existing);
                }
                require(empty, "existing_draft_requires_attention");
            }
            boolean supported = false;
            for (AccessibilityNodeInfo.AccessibilityAction action : target.getActionList()) {
                if (action.getId() == AccessibilityNodeInfo.ACTION_SET_TEXT) supported = true;
            }
            require(supported, "set_text_unavailable");
            AccessibilityNodeInfo currentRoot = root(automation);
            try {
                require(currentRoot.getWindowId() == target.getWindowId(), "window_changed");
            } finally { currentRoot.recycle(); }
            Bundle payload = new Bundle();
            payload.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            require(target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, payload),
                "set_text_rejected");
            long deadline = SystemClock.uptimeMillis() + 2000L;
            do {
                AccessibilityNodeInfo freshRoot = root(automation);
                try {
                    require(freshRoot.getWindowId() == target.getWindowId(), "window_changed");
                } finally { freshRoot.recycle(); }
                require(target.refresh(), "readback_unavailable");
                require(TARGET.contentEquals(target.getPackageName() == null ? "" : target.getPackageName())
                    && target.isEditable() && target.isVisibleToUser() && !target.isPassword(),
                    "target_changed");
                CharSequence actual = target.getText();
                // Only Android's explicit hint flag plus an exact hint value
                // proves that accessibility text represents an empty editor.
                if (android.os.Build.VERSION.SDK_INT >= 26 && target.isShowingHintText()) {
                    CharSequence hint = target.getHintText();
                    if (text.isEmpty() && hint != null && actual != null
                            && hint.toString().contentEquals(actual)) return;
                    SystemClock.sleep(100L);
                    continue;
                }
                if (text.equals(actual == null ? "" : actual.toString())) return;
                SystemClock.sleep(100L);
            } while (SystemClock.uptimeMillis() < deadline);
            throw new Rejected("readback_mismatch");
        } finally {
            if (target != null) target.recycle();
            root.recycle();
        }
    }

    private static AccessibilityNodeInfo awaitInitialRoot(UiAutomation automation)
            throws Rejected {
        long deadline = SystemClock.uptimeMillis() + 3000L;
        // UiAutomation may connect before accessibility has delivered its first window.
        // An animating window must not turn this optional settle step into a failure.
        try { automation.waitForIdle(100L, 500L); }
        catch (java.util.concurrent.TimeoutException ignored) { }
        do {
            try { return root(automation); }
            catch (Rejected failure) {
                if (!"root_unavailable".equals(failure.code)) throw failure;
            }
            long remaining = deadline - SystemClock.uptimeMillis();
            if (remaining <= 0L) break;
            SystemClock.sleep(Math.min(100L, remaining));
        } while (SystemClock.uptimeMillis() <= deadline);
        throw new Rejected("root_unavailable");
    }

    private static AccessibilityNodeInfo root(UiAutomation automation) throws Rejected {
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        require(root != null, "root_unavailable");
        if (!root.refresh()) {
            root.recycle();
            throw new Rejected("root_unavailable");
        }
        if (!TARGET.contentEquals(root.getPackageName() == null ? "" : root.getPackageName())) {
            root.recycle();
            throw new Rejected("wrong_package");
        }
        return root;
    }

    private static boolean eligible(AccessibilityNodeInfo node, Rect bounds) {
        if (!TARGET.contentEquals(node.getPackageName() == null ? "" : node.getPackageName())
                || !node.isEditable() || !node.isVisibleToUser()) return false;
        Rect actual = new Rect();
        node.getBoundsInScreen(actual);
        return bounds.equals(actual);
    }

    private static AccessibilityNodeInfo uniqueEditable(AccessibilityNodeInfo root, Rect bounds)
            throws Rejected {
        ArrayDeque<AccessibilityNodeInfo> queue = new ArrayDeque<AccessibilityNodeInfo>();
        queue.add(AccessibilityNodeInfo.obtain(root));
        AccessibilityNodeInfo match = null;
        int seen = 0;
        boolean returned = false;
        try {
            while (!queue.isEmpty()) {
                AccessibilityNodeInfo node = queue.removeFirst();
                try {
                    require(++seen <= MAX_NODES, "tree_too_large");
                    if (eligible(node, bounds)) {
                        require(match == null, "ambiguous_target");
                        match = AccessibilityNodeInfo.obtain(node);
                    }
                    int children = node.getChildCount();
                    require(children <= MAX_NODES && seen + queue.size() + children <= MAX_NODES,
                        "tree_too_large");
                    for (int i = 0; i < children; i++) {
                        AccessibilityNodeInfo child = node.getChild(i);
                        if (child != null) queue.addLast(child);
                    }
                } finally { node.recycle(); }
            }
            require(match != null, "target_not_found");
            returned = true;
            return match;
        } finally {
            while (!queue.isEmpty()) queue.removeFirst().recycle();
            if (!returned && match != null) match.recycle();
        }
    }

    private static String sha256(String text) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder(64);
        for (byte value : digest) hex.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return hex.toString();
    }

    private static void require(boolean condition, String code) throws Rejected {
        if (!condition) throw new Rejected(code);
    }

    private static final class Rejected extends Exception {
        final String code;
        Rejected(String code) { this.code = code; }
    }
}
