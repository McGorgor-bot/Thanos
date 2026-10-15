from pathlib import Path
import pandas as pd, re, hashlib
from thanos.ath import generate_ath

ATH=Path('/mnt/data/test.ath').read_bytes()

def row(el, qty, rid, name):
    return {'Element':el,'Ilość':qty,'RMS ZEST':str(rid),'RMS ID':'','Cena materiału':184.26,'J.m.':'szt.','KNR':'KNNR 5 0502-03','Opis pozycji':'Oprawa','Nazwa materiału':name,'Robocizna rg':1,'Stawka R-G':65,'Robocizna jedn.':65,'Status':'OK'}

def test_one_to_many_same_name():
    out=generate_ath(ATH,pd.DataFrame([row('A',2,613,'Jedna nazwa'),row('B',3,613,'Jedna nazwa')]))
    t=out.decode('cp1250').replace('\r\n','\n')
    assert t.count('[POZYCJA]')==2
    assert '[RMS ZEST 619]' not in t
    b=re.search(r'\[RMS ZEST 613\]\n(.*?)(?=\n\[|\Z)',t,re.S).group(1)
    assert re.search(r'^na=Jedna nazwa\t1$',b,re.M)
    assert re.search(r'^il=5$',b,re.M)

def test_one_to_many_different_names():
    out=generate_ath(ATH,pd.DataFrame([row('A',2,613,'Nazwa A'),row('B',3,613,'Nazwa B')]))
    t=out.decode('cp1250').replace('\r\n','\n')
    assert t.count('[POZYCJA]')==2
    assert '[RMS ZEST 619]' in t
    a=re.search(r'\[RMS ZEST 613\]\n(.*?)(?=\n\[|\Z)',t,re.S).group(1)
    b=re.search(r'\[RMS ZEST 619\]\n(.*?)(?=\n\[|\Z)',t,re.S).group(1)
    assert re.search(r'^na=Nazwa A\t1$',a,re.M) and re.search(r'^il=2$',a,re.M)
    assert re.search(r'^na=Nazwa B\t1$',b,re.M) and re.search(r'^il=3$',b,re.M)
    assert '[RMS 613]' in t and '[RMS 619]' in t
    body,chk=t.split('[SUMA KONTROLNA]\n',1)
    assert hashlib.md5(body.encode('cp1250')).hexdigest()==chk.strip()

if __name__=='__main__':
    test_one_to_many_same_name(); test_one_to_many_different_names(); print('ATH NAME TESTS: OK')
