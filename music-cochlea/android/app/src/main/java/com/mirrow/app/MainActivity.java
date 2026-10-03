package com.mirrow.app;
import android.app.Activity;
import android.content.*;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.webkit.*;
import android.widget.*;
import org.json.JSONObject;

/** Small runnable host for the music UI; connection values belong to the installer. */
public final class MainActivity extends Activity {
    @Override public void onCreate(Bundle state){super.onCreate(state);showConnection();}
    private void showConnection(){
        SharedPreferences prefs=getSharedPreferences("music_connection",MODE_PRIVATE);
        LinearLayout column=new LinearLayout(this);column.setOrientation(LinearLayout.VERTICAL);column.setPadding(32,40,32,32);
        TextView title=new TextView(this);title.setText("MIRROW · 共享耳蜗\n填写你自己部署的地址与令牌");title.setTextSize(21);column.addView(title);
        EditText frontend=field(column,"前端地址（http 或 https）",prefs.getString("frontend",""));
        EditText backend=field(column,"后端地址（http 或 https）",prefs.getString("backend",""));
        EditText token=field(column,"访问令牌",prefs.getString("token",""));token.setInputType(129);
        Button permission=new Button(this);permission.setText("开启通知访问");permission.setOnClickListener(v->startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));column.addView(permission);
        Button open=new Button(this);open.setText("保存并打开音乐");column.addView(open);
        open.setOnClickListener(v->{String f=frontend.getText().toString().trim(),b=backend.getText().toString().trim().replaceAll("/+$","");
            if(!valid(f)||!valid(b)||token.getText().toString().isEmpty()){Toast.makeText(this,"请填写完整地址与令牌",Toast.LENGTH_SHORT).show();return;}
            prefs.edit().putString("frontend",f).putString("backend",b).putString("token",token.getText().toString()).apply();
            if(android.os.Build.VERSION.SDK_INT>=33)requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},1);
            startForegroundService(new Intent(this,MusicRelayService.class));showWeb(f,b,token.getText().toString());});
        Button stop=new Button(this);stop.setText("断开手机音乐连接");stop.setOnClickListener(v->stopService(new Intent(this,MusicRelayService.class)));column.addView(stop);setContentView(column);
    }
    private EditText field(LinearLayout parent,String hint,String value){EditText e=new EditText(this);e.setHint(hint);e.setText(value);e.setSingleLine();parent.addView(e);return e;}
    private boolean valid(String value){try{java.net.URI u=new java.net.URI(value);return ("http".equals(u.getScheme())||"https".equals(u.getScheme()))&&u.getHost()!=null&&u.getUserInfo()==null;}catch(Exception e){return false;}}
    private void showWeb(String frontend,String backend,String token){
        WebView web=new WebView(this);web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(false);
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){return !sameOrigin(frontend,request.getUrl().toString());}
            @Override public void onPageFinished(WebView view,String url){if(sameOrigin(frontend,url))view.evaluateJavascript(
                "(()=>{const b="+JSONObject.quote(backend)+",t="+JSONObject.quote(token)+";if(localStorage.getItem('music_api')!==b||localStorage.getItem('music_token')!==t){localStorage.setItem('music_api',b);localStorage.setItem('music_token',t);location.reload();}})()",null);}
        });setContentView(web);web.loadUrl(frontend);
    }
    private boolean sameOrigin(String a,String b){try{java.net.URI x=new java.net.URI(a),y=new java.net.URI(b);return x.getScheme().equals(y.getScheme())&&x.getHost().equals(y.getHost())&&x.getPort()==y.getPort();}catch(Exception e){return false;}}
}
