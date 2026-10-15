from thanos.project_profile import consolidate_pdf_route_segments

def seg(x1,y1,x2,y2):
    return {'x1_pkt':x1,'y1_pkt':y1,'x2_pkt':x2,'y2_pkt':y2,'orientacja':'pozioma'}

def test_filter_short_label_frame_edges_and_merge_collinear():
    out=consolidate_pdf_route_segments([seg(0,10,40,10),seg(0,12,100,12),seg(100.5,11,200,11)])
    assert all(x['długość_pkt'] >= 60 for x in out)
    assert out
    assert out[0]['x1_pkt'] == 0
    assert out[0]['x2_pkt'] == 200

def test_keep_separate_parallel_routes():
    out=consolidate_pdf_route_segments([seg(0,10,100,10),seg(0,25,100,25)])
    assert len(out)==2
