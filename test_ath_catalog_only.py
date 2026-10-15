from thanos.ath import parse_ath_template, suggest_mapping, generate_ath
import pandas as pd

template='''[KOSZTORYS ATHENASOFT]\npr=NORMA\nna=Test\ndt=08.10.2026\t1\n\n[RMS ZEST 4759]\nty=M\nna=Gniazdo pojedyncze\nid=7530399\t3\njm=szt.\t000\nce=14.8\nil=0\n\n[RMS ZEST 4760]\nty=M\nna=Gniazdo pojedyncze\nid=7530299\t3\njm=szt.\t000\nce=15.86\nil=0\n\n[ELEMENT 1]\nna=Stare\n'''.encode('cp1250')
p=parse_ath_template(template)
source=pd.DataFrame({'Nazwa bloku':['GN_1F','GN_1F_IP44'],'Ilość':[69,59]})
m=suggest_mapping(source,p,'Nazwa bloku')
assert list(m['RMS ZEST'])==['4759','4760']
assert list(m['Status'])==['MATERIAŁ OK / UZUPEŁNIJ KNR']*2
assert list(m['J.m.'])==['szt.','szt.']
assert list(m['Cena materiału'])==[14.8,15.86]
out=generate_ath(template,m,'Test')
t=out.decode('cp1250')
assert t.count('[POZYCJA]')==2
assert '[RMS 4759]' in t and '[RMS 4760]' in t
assert 'ob=69' in t and 'ob=59' in t
print('PASS catalog-only RMS ZEST generation')
