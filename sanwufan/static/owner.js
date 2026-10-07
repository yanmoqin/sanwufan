'use strict';
(async()=>{
  const status=document.getElementById('status'),button=document.getElementById('stop');
  const key=`sanwufan-owner-${location.port}`,token=location.hash.slice(1)||sessionStorage.getItem(key);
  if(location.hash){sessionStorage.setItem(key,token);history.replaceState(null,'',location.pathname);}
  if(!token){status.textContent='请双击启动文件，打开属于本次服务的管理页。';return;}
  try{
    const response=await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});
    if(!response.ok)throw new Error();
    const info=await response.json(),link=document.getElementById('open-game');
    link.href=info.game_url;link.hidden=false;button.hidden=false;
    const stream=new EventSource('/events');
    stream.onmessage=()=>{status.textContent='已连接 · 可以打开牌桌开始试玩';};
    stream.onerror=()=>{status.textContent='管理连接暂时中断，正在重新连接……';};
    button.addEventListener('click',async()=>{
      button.disabled=true;
      try{
        const result=await fetch('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        if(!result.ok)throw new Error();
        stream.close();status.textContent='服务已结束';document.getElementById('tip').textContent='可以关闭本页。再次双击启动文件即可重开。';link.hidden=true;
      }catch{button.disabled=false;status.textContent='未能关闭服务，请重试或关闭启动窗口。';}
    });
    window.addEventListener('pagehide',()=>stream.close());
    window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
  }catch{status.textContent='管理服务已结束，请重新双击启动文件。';}
})();
