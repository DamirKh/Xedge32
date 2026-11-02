-- ESP-NOW stability receiver for Xedge32 LuaShell32.
-- host.py loads this file line by line, so keep every line short.
st={}
st.rx=0
st.uniq=0
st.dup=0
st.oor=0
st.bad=0
st.lenbad=0
st.first=0
st.last=0
st.lastnew=0
st.maxgap=0
st.rssi_min=0
st.rssi_max=-127
st.rssi_sum=0
st.seen={}
st.chan=0
st.chanchg=0
st.plen=32
function rxseq(d) local a,b,c,e=d:byte(5,8) return a+b*256+c*65536+e*16777216 end
function rxprune() local lim=st.last-4096 for k in pairs(st.seen) do if k<lim then st.seen[k]=nil end end end
function rxchan(c) if st.chan==0 then st.chan=c return end if c~=st.chan then st.chanchg=st.chanchg+1 st.chan=c end end
function rxrssi(r) st.rssi_sum=st.rssi_sum+r if r<st.rssi_min then st.rssi_min=r end if r>st.rssi_max then st.rssi_max=r end end
function rxfresh(q) if st.first==0 then st.first=q st.last=q st.lastnew=q end end
function rxgap(q) local g=q-st.lastnew-1 if g>st.maxgap then st.maxgap=g end st.lastnew=q st.last=q end
function rxadd(q) if st.seen[q] then st.dup=st.dup+1 return end st.seen[q]=true st.uniq=st.uniq+1 if q<st.last then st.oor=st.oor+1 end if q>st.last then rxgap(q) end end
function rxone(d,i) local q=rxseq(d) rxfresh(q) if i.len~=st.plen then st.lenbad=st.lenbad+1 end rxrssi(i.rssi) rxchan(i.channel) rxadd(q) end
function onRx(d,s,i) if #d<8 or d:sub(1,4)~="XNW1" then st.bad=st.bad+1 return end st.rx=st.rx+1 rxone(d,i) if st.rx%1024==0 then rxprune() end end
function rxavg() if st.rx>0 then return st.rssi_sum/st.rx end return 0 end
function rxdrops() if rx then return rx:info().drops end return 0 end
function rxreport(tag) local a=string.format("%.1f",rxavg()) print("STAT",tag,st.rx,st.uniq,st.dup,st.oor,st.maxgap) print("STAT2",st.rssi_min,a,st.rssi_max,st.bad,st.lenbad,st.chan,st.chanchg,rxdrops()) end
