// Local screenshot comparison. Only a unique, newly observed image ID may win.
(() => {
  const texture = pixels => {
    const gray=[];
    for(let i=0;i<pixels.length;i+=4)gray.push((pixels[i]+pixels[i+1]+pixels[i+2])/3);
    const mean=gray.reduce((a,b)=>a+b,0)/gray.length;
    return {gray,mean,std:Math.sqrt(gray.reduce((sum,v)=>sum+(v-mean)**2,0)/gray.length)};
  };
  function compare(a,b) {
    if(a.length!==4096 || b.length!==4096)return null;
    const x=texture(a),y=texture(b);
    if(x.std<12 || y.std<12)return null; // Flat/blank thumbnails are not identity.
    let difference=0,covariance=0,edges=0,agree=0;
    for(let i=0;i<x.gray.length;i++) {
      covariance+=(x.gray[i]-x.mean)*(y.gray[i]-y.mean);
      for(let c=0;c<3;c++)difference+=Math.abs(a[i*4+c]-b[i*4+c]);
      if(i%32<31) {
        const dx=x.gray[i+1]-x.gray[i],dy=y.gray[i+1]-y.gray[i];
        if(Math.abs(dx)>5) {edges++;if(dx*dy>0)agree++;}
      }
    }
    return {error:difference/(x.gray.length*3*255),correlation:covariance/(x.gray.length*x.std*y.std),
      edges:edges>=20?agree/edges:0};
  }
  function choose(scored,before) {
    const ordered=scored.filter(item=>item.score).sort((a,b)=>a.score.error-b.score.error);
    const best=ordered[0],second=ordered[1];
    if(!best || before.includes(best.media_id) || best.score.error>.055 || best.score.correlation<.96 || best.score.edges<.9 ||
       second && second.score.error-best.score.error<.025)return null;
    return {media_id:best.media_id,source:'local_screenshot',score:best.score};
  }
  async function match(reference,candidates,before,capture) {
    const source=await createImageBitmap(await (await fetch(reference.thumbnail)).blob());
    let screen;
    try {
      screen=await createImageBitmap(await (await fetch('data:image/png;base64,'+capture.data)).blob());
      const canvas=new OffscreenCanvas(32,32),ctx=canvas.getContext('2d',{willReadFrequently:true});
      const sx=screen.width/capture.width,sy=screen.height/capture.height,scored=[];
      for(const item of candidates) {
        let left=0,top=0,width=source.width,height=source.height;
        if(item.fit==='cover') {
          const aspect=item.width/item.height;
          if(width/height>aspect) {left=(width-height*aspect)/2;width=height*aspect;}
          else {top=(height-width/aspect)/2;height=width/aspect;}
        }
        ctx.clearRect(0,0,32,32);ctx.drawImage(source,left,top,width,height,0,0,32,32);
        const expected=ctx.getImageData(0,0,32,32).data;
        ctx.clearRect(0,0,32,32);ctx.drawImage(screen,item.x*sx,item.y*sy,item.width*sx,item.height*sy,0,0,32,32);
        scored.push({media_id:item.media_id,score:compare(expected,ctx.getImageData(0,0,32,32).data)});
      }
      return choose(scored,before);
    } finally {source.close();screen?.close();}
  }
  globalThis.__lzxVisualUpload={compare,choose,match};
})();
