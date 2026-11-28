package com.mirrow.app.widget;

import android.content.Context;
import android.content.res.TypedArray;
import android.view.View;
import android.widget.RemoteViews;

import com.mirrow.app.R;

final class WanderArtwork {
    private static final int[] FRAME_VIEWS = {
        R.id.wander_frame_0, R.id.wander_frame_1, R.id.wander_frame_2,
        R.id.wander_frame_3, R.id.wander_frame_4, R.id.wander_frame_5
    };

    static void show(Context context, RemoteViews views, String scene) {
        String pkg = context.getPackageName();
        int frames = context.getResources().getIdentifier("wander_frames_" + scene, "array", pkg);
        views.removeAllViews(R.id.wander_motion_slot);
        if (frames == 0) {
            int art = context.getResources().getIdentifier("wk_" + scene, "drawable", pkg);
            views.setImageViewResource(R.id.wander_art, art == 0 ? R.drawable.wk_unavailable : art);
            views.setViewVisibility(R.id.wander_art, View.VISIBLE);
            return;
        }
        RemoteViews motion = new RemoteViews(pkg, R.layout.wander_motion);
        TypedArray sequence = context.getResources().obtainTypedArray(frames);
        try {
            for (int i = 0; i < FRAME_VIEWS.length; i++) {
                motion.setImageViewResource(FRAME_VIEWS[i], sequence.getResourceId(i, 0));
            }
        } finally {
            sequence.recycle();
        }
        views.setViewVisibility(R.id.wander_art, View.GONE);
        views.addView(R.id.wander_motion_slot, motion);
    }

    enum Mood {
        JOY("愉悦", "☀️", R.drawable.wander_bg_fern, 0x30FBB142),
        AFFECTION("亲近", "🌹", R.drawable.wander_bg_fern, 0x46ED888D),
        CALM("安定", "🌿", R.drawable.wander_bg_fern, 0),
        INTEREST("兴趣", "🔹", R.drawable.wander_bg_atlas, 0),
        SAD("低落", "🌧️", R.drawable.wander_bg_rain, 0),
        WORRY("担忧", "🫧", R.drawable.wander_bg_rain, 0x4436566B),
        ANGER("恼怒", "🔥", R.drawable.wander_bg_atlas, 0x527F352B),
        AVERSION("排斥", "🫒", R.drawable.wander_bg_fern, 0x66524C3D),
        SURPRISE("惊讶", "✨", R.drawable.wander_bg_atlas, 0x405C4489),
        COMPLEX("", "🌗", R.drawable.wander_bg_rain, 0x403C395B),
        UNKNOWN("", "", R.drawable.wander_bg_atlas, 0x99414953);

        final String family, icon;
        final int background, tint;

        Mood(String family, String icon, int background, int tint) {
            this.family = family;
            this.icon = icon;
            this.background = background;
            this.tint = tint;
        }

        static Mood from(String family) {
            if (family.isEmpty()) return UNKNOWN;
            for (Mood mood : values()) if (family.equals(mood.family)) return mood;
            return COMPLEX;
        }
    }

    private WanderArtwork() {}
}
