package com.mirrow.app;
import android.app.*;
import android.content.*;
import android.os.IBinder;
import org.json.JSONObject;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;

/** Authenticated transport for mobile_music_control only. */
public final class MusicRelayService extends Service {
    private volatile boolean running;
    private ExecutorService executor;
    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager=getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("music_relay","音乐连接",NotificationManager.IMPORTANCE_LOW));
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE);
        startForeground(1,new Notification.Builder(this,"music_relay").setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle("MIRROW 共享耳蜗").setContentText("音乐控制连接已开启").setContentIntent(open).build());
        NeteasePlaybackGuard.restore(this);
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId) {
        if(!running) {
            running=true;executor=Executors.newSingleThreadExecutor();executor.submit(()->{
                while(running) {
                    try {
                        JSONObject command=request("/api/music-relay/next",null);
                        String id=command.optString("request_id","");
                        if(id.isEmpty()||id.equals("null"))continue;
                        JSONObject params=command.optJSONObject("params");
                        JSONObject result=NeteaseMusicController.execute(this,params);
                        request("/api/music-relay/result",new JSONObject().put("request_id",id).put("result",result));
                    } catch(Exception ignored) {
                        try{Thread.sleep(1500);}catch(InterruptedException end){break;}
                    }
                }
            });
        }
        return START_STICKY;
    }
    private JSONObject request(String path,JSONObject body)throws Exception {
        SharedPreferences prefs=getSharedPreferences("music_connection",MODE_PRIVATE);
        String token=prefs.getString("token","");
        if(token.isEmpty())throw new IllegalStateException("Set an access token first");
        String base=prefs.getString("backend","");
        HttpURLConnection conn=(HttpURLConnection)new URL(base+path).openConnection();
        conn.setConnectTimeout(6000);conn.setReadTimeout(10000);conn.setInstanceFollowRedirects(false);
        conn.setRequestProperty("Authorization","Bearer "+token);
        try {
            if(body!=null){conn.setRequestMethod("POST");conn.setDoOutput(true);conn.setRequestProperty("Content-Type","application/json");
                try(java.io.OutputStream out=conn.getOutputStream()){out.write(body.toString().getBytes(StandardCharsets.UTF_8));}}
            if(conn.getResponseCode()!=200)throw new java.io.IOException("Music relay unavailable");
            try(java.io.InputStream in=conn.getInputStream();java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream()){
                byte[] bytes=new byte[4096];int n;while((n=in.read(bytes))!=-1){out.write(bytes,0,n);}
                return new JSONObject(out.toString("UTF-8"));
            }
        }finally{conn.disconnect();}
    }
    @Override public void onDestroy(){running=false;if(executor!=null)executor.shutdownNow();super.onDestroy();}
}
