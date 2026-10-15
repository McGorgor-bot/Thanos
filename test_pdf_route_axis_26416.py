from thanos.project_profile import consolidate_pdf_route_segments


def test_merge_collinear_fragments_and_parallel_edges():
    segs = [
        {'x1_pkt': 0, 'y1_pkt': 10, 'x2_pkt': 34, 'y2_pkt': 10, 'długość_pkt': 34, 'orientacja': 'pozioma'},
        {'x1_pkt': 34, 'y1_pkt': 10, 'x2_pkt': 70, 'y2_pkt': 10, 'długość_pkt': 36, 'orientacja': 'pozioma'},
        {'x1_pkt': 0, 'y1_pkt': 12, 'x2_pkt': 70, 'y2_pkt': 12, 'długość_pkt': 70, 'orientacja': 'pozioma'},
    ]
    result = consolidate_pdf_route_segments(segs)
    assert len(result) == 1
    assert result[0]['długość_pkt'] == 70
    assert result[0]['liczba_scalonych_krawędzi'] == 3


def test_keep_separate_parallel_runs_outside_tolerance():
    segs = [
        {'x1_pkt': 0, 'y1_pkt': 10, 'x2_pkt': 80, 'y2_pkt': 10, 'długość_pkt': 80, 'orientacja': 'pozioma'},
        {'x1_pkt': 0, 'y1_pkt': 40, 'x2_pkt': 80, 'y2_pkt': 40, 'długość_pkt': 80, 'orientacja': 'pozioma'},
    ]
    assert len(consolidate_pdf_route_segments(segs)) == 2
