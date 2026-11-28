package com.mirrow.app.widget;

import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProviderInfo;
import android.content.ComponentName;
import android.content.Context;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.ImageView;
import android.widget.ViewFlipper;
import androidx.test.platform.app.InstrumentationRegistry;
import com.mirrow.app.R;
import org.json.JSONObject;
import org.junit.Test;
import java.util.Scanner;
import static org.junit.Assert.*;

public class WanderWidgetTest {
    private final Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();

    @Test public void refreshSharesTheActivityRowAtPhoneAndCompactSizes() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            float density = context.getResources().getDisplayMetrics().density;
            for (int[] size : new int[][]{{320, 172}, {320, 127}, {218, 100}, {205, 58}}) {
                int w = Math.round(size[0] * density), h = Math.round(size[1] * density);
                View root = WanderWidget.views(context, "13:23 所见", "在等你的消息…", "",
                        "phone_wait", "亲近", WanderWidget.compactLayout(size[0], size[1]))
                        .apply(context, new FrameLayout(context));
                root.measure(View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.EXACTLY),
                        View.MeasureSpec.makeMeasureSpec(h, View.MeasureSpec.EXACTLY));
                root.layout(0, 0, w, h);
                View refresh = root.findViewById(R.id.wander_refresh);
                View activity = root.findViewById(R.id.wander_activity);
                View bubble = root.findViewById(R.id.wander_bubble);
                assertSame(activity.getParent(), refresh.getParent());
                assertEquals(activity.getHeight(), refresh.getHeight());
                assertTrue(refresh.getLeft() > activity.getRight());
                assertTrue(bubble.getWidth() >= .27 * w && bubble.getWidth() <= .30 * w);
                android.graphics.Rect bounds = new android.graphics.Rect(0, 0, refresh.getWidth(), refresh.getHeight());
                ((android.view.ViewGroup) root).offsetDescendantRectToMyCoords(refresh, bounds);
                assertTrue(bounds.bottom <= h);
            }
        });
    }

    @Test public void providerAndRemoteViewsAreUsableByLauncher() {
        ComponentName expected = new ComponentName(context, WanderWidget.class);
        AppWidgetProviderInfo info = AppWidgetManager.getInstance(context).getInstalledProviders().stream()
                .filter(item -> expected.equals(item.provider)).findFirst().orElse(null);
        assertNotNull(info);
        assertEquals(R.layout.wander_widget, info.initialLayout);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            FrameLayout parent = new FrameLayout(context);
            View view = WanderWidget.views(context, "17:10 所见", "正在听歌", "", "listen_music", "安定", false).apply(context, parent);
            assertEquals("正在听歌", ((TextView) view.findViewById(R.id.wander_label)).getText());
            assertEquals(View.GONE, view.findViewById(R.id.wander_detail).getVisibility());
            assertTrue(view.findViewById(R.id.wander_refresh).hasOnClickListeners());
            assertTrue(view.hasOnClickListeners());
            ViewFlipper motion = view.findViewById(R.id.wander_motion);
            assertTrue(motion.isAutoStart());
            assertEquals(6, motion.getChildCount());
            for (int i = 0; i < motion.getChildCount(); i++) {
                assertNotNull(((ImageView) motion.getChildAt(i)).getDrawable());
            }
            WanderWidget.views(context, "17:11 所见", "在回顾刚才的活动", "听歌", "node_review", "", true).reapply(context, view);
            assertNull(view.findViewById(R.id.wander_motion));
            assertEquals(View.VISIBLE, view.findViewById(R.id.wander_art).getVisibility());
        });
    }

    @Test public void waitReviewAndSocialPriorityUseTheActualNode() throws Exception {
        JSONObject catalog;
        try (Scanner source = new Scanner(context.getAssets().open("activity-visuals.json"), "UTF-8")) {
            catalog = new JSONObject(source.useDelimiter("\\A").next());
        }
        JSONObject state = new JSONObject().put("runtime_state","active").put("current_event_type","listen_music");
        JSONObject data = new JSONObject().put("wander",state);
        assertEquals("正在听歌", WanderBadge.from(data, catalog, new JSONObject()).label);
        assertEquals("listen_music", WanderBadge.from(data, catalog, new JSONObject()).scene);
        state.put("current_activity_state","active_wait").put("next_node_at","2026-11-26T18:43:00");
        assertEquals("听歌 · 18:43", WanderBadge.from(data, catalog, new JSONObject()).detail);
        assertEquals("node_wait", WanderBadge.from(data, catalog, new JSONObject()).scene);
        state.put("current_activity_state","node_review");
        assertEquals("在回顾刚才的活动", WanderBadge.from(data, catalog, new JSONObject()).label);
        assertEquals("node_review", WanderBadge.from(data, catalog, new JSONObject()).scene);
        state.put("current_activity_state","settling");
        assertEquals("在整理活动感想", WanderBadge.from(data, catalog, new JSONObject()).label);
        assertEquals("settling", WanderBadge.from(data, catalog, new JSONObject()).scene);
        state.put("runtime_state","planning");
        assertEquals("在安排接下来的活动", WanderBadge.from(data, catalog, new JSONObject()).label);
        state.put("runtime_state","scheduled");
        assertEquals("发呆中…", WanderBadge.from(data, catalog, new JSONObject()).label);
        state.put("wake_reason","planner_timeout");
        assertEquals("等待重试", WanderBadge.from(data, catalog, new JSONObject()).label);
        state.put("runtime_state","idle").put("mode","offline");
        assertEquals("此刻未在漫想", WanderBadge.from(data, catalog, new JSONObject()).label);
        data.put("primary_reply_running",false);
        assertEquals("phone_wait", WanderBadge.from(data, catalog, new JSONObject()).scene);
        data.put("primary_reply_running",true);
        assertEquals("phone_reply", WanderBadge.from(data, catalog, new JSONObject()).scene);
        data.put("primary_reply_running",false);
        assertEquals("状态待同步", WanderBadge.from(new JSONObject(), catalog, new JSONObject()).label);
    }
}
