"""Lettore minimale di database Microsoft Access (Jet 4, .mdb) in Python puro — usato per importare ZKAccess."""
import struct, datetime, sys
PS=4096
class DB:
    def __init__(s,path):
        s.b=open(path,'rb').read(); s.np=len(s.b)//PS
    def pg(s,n): return s.b[n*PS:(n+1)*PS]
    def tdef(s,p):
        d=s.pg(p); assert d[0]==2,(p,d[0])
        body=bytearray(d); nxt=struct.unpack_from('<I',d,4)[0]
        while nxt:
            d2=s.pg(nxt); body+=d2[8:]; nxt=struct.unpack_from('<I',d2,4)[0]
        b=bytes(body)
        ncols=struct.unpack_from('<H',b,45)[0]; nridx=struct.unpack_from('<I',b,51)[0]
        o=63+nridx*12; cols=[]
        for i in range(ncols):
            c=b[o:o+25]; o+=25
            cols.append(dict(type=c[0],num=struct.unpack_from('<H',c,5)[0],var=struct.unpack_from('<H',c,7)[0],
              flags=c[15],fixoff=struct.unpack_from('<H',c,21)[0],size=struct.unpack_from('<H',c,23)[0]))
        for c in cols:
            l=struct.unpack_from('<H',b,o)[0]; c['name']=b[o+2:o+2+l].decode('utf-16le'); o+=2+l
        return cols
    def text(s,d):
        if d[:2]==b'\xff\xfe':
            out=[];comp=True;i=2
            while i<len(d):
                if d[i]==0: comp=not comp;i+=1;continue
                if comp: out.append(chr(d[i]));i+=1
                else: out.append(d[i:i+2].decode('utf-16le','replace'));i+=2
            return ''.join(out)
        return d.decode('utf-16le','replace')
    def rowat(s,pg,r):
        d=s.pg(pg); n=struct.unpack_from('<H',d,12)[0]
        if r>=n: return None
        off=struct.unpack_from('<H',d,14+2*r)[0]
        end=PS if r==0 else struct.unpack_from('<H',d,14+2*(r-1))[0]&0x1fff
        return off, d[off&0x1fff:end]
    def memo(s,d):
        ln=struct.unpack_from('<I',d,0)[0]; fl=ln>>24; ln&=0xffffff
        if fl&0x80: return d[12:12+ln]
        ptr=struct.unpack_from('<I',d,4)[0]; out=b''
        while ptr and len(out)<ln:
            r=s.rowat(ptr>>8,ptr&0xff)
            if not r: break
            data=r[1]
            if fl&0x40: return data[:ln]
            ptr=struct.unpack_from('<I',data,0)[0]; out+=data[4:]
        return out[:ln]
    def rows(s,name):
        tp=s.tables()[name]; cols=s.tdef(tp)
        for p in range(s.np):
            d=s.pg(p)
            if d[0]!=1 or struct.unpack_from('<I',d,4)[0]!=tp: continue
            n=struct.unpack_from('<H',d,12)[0]
            for r in range(n):
                off=struct.unpack_from('<H',d,14+2*r)[0]
                if off&0x8000: continue
                end=PS if r==0 else struct.unpack_from('<H',d,14+2*(r-1))[0]&0x1fff
                row=d[off&0x1fff:end]
                if off&0x4000:
                    ptr=struct.unpack_from('<I',row,0)[0]; rr=s.rowat(ptr>>8,ptr&0xff)
                    if not rr: continue
                    row=rr[1]
                try: yield s.parse(row,cols)
                except Exception: pass
    def parse(s,row,cols):
        nc=struct.unpack_from('<H',row,0)[0]; L=len(row)
        bm=(nc+7)//8; bits=row[L-bm:]
        nvar=struct.unpack_from('<H',row,L-bm-2)[0]
        voffs=[struct.unpack_from('<H',row,L-bm-4-2*i)[0] for i in range(nvar+1)]
        out={}
        for c in cols:
            present=(bits[c['num']//8]>>(c['num']%8))&1 if c['num']<nc else 0
            t=c['type']
            if t==1: out[c['name']]=bool(present); continue
            if not present: out[c['name']]=None; continue
            if c['flags']&1:
                o=2+c['fixoff']; v=row[o:o+c['size']]
            else:
                if c['var']>=nvar: out[c['name']]=None; continue
                v=row[voffs[c['var']]:voffs[c['var']+1]]
            if t==2: val=v[0]
            elif t==3: val=struct.unpack('<h',v[:2])[0]
            elif t==4: val=struct.unpack('<i',v[:4])[0]
            elif t==5: val=struct.unpack('<q',v[:8])[0]/10000
            elif t==6: val=struct.unpack('<f',v[:4])[0]
            elif t==7: val=struct.unpack('<d',v[:8])[0]
            elif t==8:
                x=struct.unpack('<d',v[:8])[0]
                try: val=(datetime.datetime(1899,12,30)+datetime.timedelta(days=x)).isoformat(' ')
                except: val=x
            elif t==10: val=s.text(v)
            elif t==12:
                m=s.memo(v); val=s.text(m)
            elif t==11: val=('<blob %d>'%len(s.memo(v)))
            else: val=v.hex()
            out[c['name']]=val
        return out
    _t=None
    def tables(s):
        if s._t: return s._t
        cols=s.tdef(2); s._t={}
        for p in range(s.np):
            d=s.pg(p)
            if d[0]!=1 or struct.unpack_from('<I',d,4)[0]!=2: continue
            n=struct.unpack_from('<H',d,12)[0]
            for r in range(n):
                off=struct.unpack_from('<H',d,14+2*r)[0]
                if off&0x8000: continue
                end=PS if r==0 else struct.unpack_from('<H',d,14+2*(r-1))[0]&0x1fff
                row=d[off&0x1fff:end]
                if off&0x4000:
                    ptr=struct.unpack_from('<I',row,0)[0]; rr=s.rowat(ptr>>8,ptr&0xff)
                    if not rr: continue
                    row=rr[1]
                try: o=s.parse(row,cols)
                except: continue
                if o.get('Type')==1: s._t[o['Name']]=o['Id']&0xffffff
        return s._t
if __name__ == "__main__":
    db=DB(sys.argv[1])
    if len(sys.argv)==2: print(sorted(db.tables()))
    else:
        for r in db.rows(sys.argv[2]): print(r)
