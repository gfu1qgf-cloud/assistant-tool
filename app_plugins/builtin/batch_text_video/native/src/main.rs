use std::{env, fs, io::{Read, Seek, SeekFrom, Write}};

fn alpha(packet: &[u8]) -> Result<(u32,u32,Vec<u8>),Box<dyn std::error::Error>> {
    // Reject oversized or unsupported headers before the decoder allocates planes.
    let header=veloster_dnx::probe(packet)?;
    if !header.alpha || header.premultiplied_alpha || header.interlaced ||
       !matches!(header.cid,1270..=1274) || header.width>8192 || header.lines>8192 ||
       u64::from(header.width)*u64::from(header.lines)>40_000_000 {
        return Err("Unsupported DNxHR alpha header".into());
    }
    let frame=veloster_dnx::decode_frame(packet)?;
    let plane=frame.alpha.as_ref().ok_or("Missing alpha plane")?;
    if frame.width!=header.width || frame.height!=header.lines ||
       plane.len()!=frame.width as usize*frame.height as usize || !matches!(frame.bit_depth,8|10|12) {
        return Err("Inconsistent decoded alpha".into());
    }
    let max=(1u32<<frame.bit_depth)-1;
    Ok((frame.width,frame.height,plane.iter().map(|v|((u32::from(*v).min(max)*255+max/2)/max) as u8).collect()))
}

fn run() -> Result<(),Box<dyn std::error::Error>> {
    let args:Vec<String>=env::args().collect();
    if args.len()!=4 {return Err("Usage: dnx-alpha --frame packet.dnx output.pgm | --stream source.mov index.bin".into());}
    if args[1]=="--frame" {
        if fs::metadata(&args[2])?.len()>64*1024*1024 {return Err("Packet too large".into());}
        let (w,h,plane)=alpha(&fs::read(&args[2])?)?;
        let mut output=fs::File::create(&args[3])?;
        output.write_all(format!("P5\n{} {}\n255\n",w,h).as_bytes())?;output.write_all(&plane)?;
    } else if args[1]=="--stream" {
        if fs::metadata(&args[3])?.len()>1_200_008 {return Err("Index too large".into());}
        let index=fs::read(&args[3])?;
        if index.len()<8 || &index[0..4]!=b"DNA1" {return Err("Invalid index".into());}
        let count=u32::from_le_bytes(index[4..8].try_into()?) as usize;
        if count==0 || count>100_000 || index.len()!=8+count*12 {return Err("Invalid frame count".into());}
        let mut source=fs::File::open(&args[2])?;let length=source.metadata()?.len();
        let mut output=std::io::stdout().lock();let mut shape=None;let mut packet=Vec::new();
        for i in 0..count {
            let at=8+i*12;let pos=u64::from_le_bytes(index[at..at+8].try_into()?);
            let size=u32::from_le_bytes(index[at+8..at+12].try_into()?) as usize;
            if !(640..=64*1024*1024).contains(&size) || pos.checked_add(size as u64).ok_or("Position overflow")?>length {
                return Err("Packet outside source".into());
            }
            source.seek(SeekFrom::Start(pos))?;packet.resize(size,0);source.read_exact(&mut packet)?;
            let (w,h,plane)=alpha(&packet)?;
            if shape.is_some_and(|s|s!=(w,h)) {return Err("Video dimensions changed".into());}
            shape=Some((w,h));output.write_all(&plane)?;
        }
    } else {return Err("Unknown command".into());}
    Ok(())
}

fn main() {
    if let Err(error)=run() {eprintln!("{}",error);std::process::exit(1);}
}
