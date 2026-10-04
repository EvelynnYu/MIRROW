import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {SocialForwardCard,SocialForwardDialog} from '../src/components/SocialForward';
import '../src/pages/SocialFeedPage.css';

let sourceVisible=true;
const source={id:'source-test',author:'visitor:person_a',content:'演示公开动态：用于测试转发。',visibility:'public',created_at:1,people:{'visitor:person_a':{name:'人甲'}}};
let packet='';
window.fetch=async(input,init)=>{
  const path=String(input);
  if(path.endsWith('/api/social-sites'))return Response.json({sites:[{id:'person_a',name:'示例家庭',enabled:true,has_human_key:true}]});
  if(path.includes('/moments/'))return sourceVisible?Response.json(source):Response.json({detail:'moment_not_found'},{status:404});
  if(path.endsWith('/forward')){packet=String(init?.body);return Response.json({id:'forward-test'});}
  throw Error('隔离测试禁止其他API请求');
};
function Fixture(){
  const [dialog,setDialog]=useState(false),[visible,setVisible]=useState(true),[sent,setSent]=useState('');
  return <div style={{maxWidth:460,margin:'30px auto',padding:18,fontFamily:'system-ui',background:'#fffaf5',color:'#443d3a',borderRadius:22}}><small>仅虚构资料 · API全部拦截在内存</small><h2>共域 · 转发验收</h2><article><strong>AI · 本家机</strong><p>看到这条，替你高兴。</p><SocialForwardCard name={()=>'人甲'} forward={{reference:{origin:'',moment_id:source.id},status:visible?'available':'unavailable',...(visible?{source}: {})}}/></article>
    <SocialForwardCard name={()=>'人甲'} forward={{reference:{origin:'https://example.test',moment_id:'remote-source'},status:'link_only'}}/>
    <button onClick={()=>{sourceVisible=false;setVisible(false);}}>模拟源帖转私密</button><button onClick={()=>setDialog(true)}>打开转发弹窗</button>
    {dialog&&<SocialForwardDialog momentId={source.id} onClose={()=>setDialog(false)} onDone={()=>setSent(packet)}/>}
    <pre data-testid="submitted" style={{whiteSpace:'pre-wrap',fontSize:12}}>{sent}</pre>
  </div>;
}
createRoot(document.getElementById('root')!).render(<Fixture/>);
