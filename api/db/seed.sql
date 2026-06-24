-- ── Disrupted flights ─────────────────────────────────────────────────────
INSERT OR IGNORE INTO FLIGHT_LEG (flight_num, origin_apt, dest_apt, std, eta, flight_status, aircraft_type)
VALUES
    ('QF400', 'SYD', 'MEL', '2025-07-14T11:00:00', '2025-07-14T12:30:00', 'CX', 'B737'),
    ('QF731', 'SYD', 'BNE', '2025-07-14T12:00:00', '2025-07-14T13:45:00', 'DL', 'A320'),
    ('QF8',   'SYD', 'LHR', '2025-07-14T16:00:00', '2025-07-15T05:30:00', 'CX', 'A380'),
    ('QF500', 'MEL', 'SYD', '2025-07-14T10:00:00', '2025-07-14T11:15:00', 'CX', 'B737');

-- ── Alternate flights ──────────────────────────────────────────────────────
INSERT OR IGNORE INTO FLIGHT_LEG (flight_num, origin_apt, dest_apt, std, eta, flight_status, aircraft_type)
VALUES
    ('QF402', 'SYD', 'MEL', '2025-07-14T14:30:00', '2025-07-14T16:00:00', 'SC', 'B737'),
    ('QF404', 'SYD', 'MEL', '2025-07-14T16:00:00', '2025-07-14T17:30:00', 'SC', 'B737'),
    ('QF733', 'SYD', 'BNE', '2025-07-14T15:00:00', '2025-07-14T16:45:00', 'SC', 'A320'),
    ('QF10',  'SYD', 'LHR', '2025-07-15T11:00:00', '2025-07-16T05:00:00', 'SC', 'A380'),
    ('QF502', 'MEL', 'SYD', '2025-07-14T13:00:00', '2025-07-14T14:15:00', 'SC', 'B737'),
    ('QF504', 'MEL', 'SYD', '2025-07-14T15:30:00', '2025-07-14T16:45:00', 'SC', 'A320');

-- ── Inventory for alternate flights ───────────────────────────────────────
INSERT OR IGNORE INTO FLIGHT_INVENTORY (flight_num, class_of_service, seats_available)
VALUES
    ('QF402', 'J',  4), ('QF402', 'W',  8), ('QF402', 'Y', 32),
    ('QF404', 'J',  2), ('QF404', 'W',  6), ('QF404', 'Y', 45),
    ('QF733', 'J',  3), ('QF733', 'W', 10), ('QF733', 'Y', 50),
    ('QF10',  'F',  2), ('QF10',  'J',  6), ('QF10',  'W', 12), ('QF10', 'Y', 60),
    ('QF502', 'J',  1), ('QF502', 'W',  4), ('QF502', 'Y', 20),
    ('QF504', 'J',  2), ('QF504', 'W',  8), ('QF504', 'Y', 40);

-- ── Scenario A: QF400 SYD→MEL — Platinum happy path ──────────────────────
INSERT OR IGNORE INTO PASSENGER_PROFILE (pax_id, first_name, last_name, qff_number, tier_status, ssr_codes)
VALUES ('PAX-001', 'Eleanor', 'Chen', 'QF1000001', 'Platinum', NULL);

INSERT OR IGNORE INTO PNR_HEADER (pnr_locator, booking_status, created_at)
VALUES ('AABBCC', 'HK', '2025-06-01T09:00:00');

INSERT OR IGNORE INTO PASSENGER_SEGMENT (segment_id, pnr_locator, pax_id, flight_num, ticket_num, class_of_service)
VALUES ('SEG-A-001', 'AABBCC', 'PAX-001', 'QF400', 'TKT-A-001', 'J');

INSERT OR IGNORE INTO IROPS_LOG (irops_id, flight_num, iata_delay_code, triggered_at, resolution_status, is_custom)
VALUES ('IROPS-A-001', 'QF400', 41, '2025-07-14T10:15:00', 'pending', 0);

-- ── Scenario B: QF731 SYD→BNE — Bronze, weather delay (hotel blocked) ────
INSERT OR IGNORE INTO PASSENGER_PROFILE (pax_id, first_name, last_name, qff_number, tier_status, ssr_codes)
VALUES ('PAX-002', 'Marcus', 'Webb', 'QF2000002', 'Bronze', NULL);

INSERT OR IGNORE INTO PNR_HEADER (pnr_locator, booking_status, created_at)
VALUES ('DDEEFF', 'HK', '2025-06-15T14:00:00');

INSERT OR IGNORE INTO PASSENGER_SEGMENT (segment_id, pnr_locator, pax_id, flight_num, ticket_num, class_of_service)
VALUES ('SEG-B-001', 'DDEEFF', 'PAX-002', 'QF731', 'TKT-B-001', 'Y');

INSERT OR IGNORE INTO IROPS_LOG (irops_id, flight_num, iata_delay_code, triggered_at, resolution_status, is_custom)
VALUES ('IROPS-B-001', 'QF731', 71, '2025-07-14T11:30:00', 'pending', 0);

-- ── Scenario C: QF8 SYD→LHR — Gold + WCHR (SSR hard stop) ───────────────
INSERT OR IGNORE INTO PASSENGER_PROFILE (pax_id, first_name, last_name, qff_number, tier_status, ssr_codes)
VALUES ('PAX-003', 'Robert', 'Nguyen', 'QF3000003', 'Gold', 'WCHR');

INSERT OR IGNORE INTO PNR_HEADER (pnr_locator, booking_status, created_at)
VALUES ('GGHHII', 'HK', '2025-05-20T08:00:00');

INSERT OR IGNORE INTO PASSENGER_SEGMENT (segment_id, pnr_locator, pax_id, flight_num, ticket_num, class_of_service)
VALUES ('SEG-C-001', 'GGHHII', 'PAX-003', 'QF8', 'TKT-C-001', 'J');

INSERT OR IGNORE INTO IROPS_LOG (irops_id, flight_num, iata_delay_code, triggered_at, resolution_status, is_custom)
VALUES ('IROPS-C-001', 'QF8', 41, '2025-07-14T14:00:00', 'pending', 0);

-- ── Scenario D: QF500 MEL→SYD — 12 passengers (budget cap) ──────────────
INSERT OR IGNORE INTO PASSENGER_PROFILE (pax_id, first_name, last_name, qff_number, tier_status, ssr_codes)
VALUES
    ('PAX-D-01', 'Sophia',   'Park',    'QF4000001', 'Platinum', NULL),
    ('PAX-D-02', 'James',    'Liu',     'QF4000002', 'Platinum', NULL),
    ('PAX-D-03', 'Aisha',    'Rahman',  'QF4000003', 'Gold',     NULL),
    ('PAX-D-04', 'Thomas',   'Kovacs',  'QF4000004', 'Gold',     NULL),
    ('PAX-D-05', 'Priya',    'Sharma',  'QF4000005', 'Gold',     NULL),
    ('PAX-D-06', 'Daniel',   'OBrien',  'QF4000006', 'Silver',   NULL),
    ('PAX-D-07', 'Yuki',     'Tanaka',  'QF4000007', 'Silver',   NULL),
    ('PAX-D-08', 'Carlos',   'Mendez',  'QF4000008', 'Silver',   NULL),
    ('PAX-D-09', 'Hannah',   'Fischer', 'QF4000009', 'Bronze',   NULL),
    ('PAX-D-10', 'Ahmed',    'Hassan',  'QF4000010', 'Bronze',   NULL),
    ('PAX-D-11', 'Lena',     'Mueller', 'QF4000011', 'Bronze',   NULL),
    ('PAX-D-12', 'Benjamin', 'Clarke',  'QF4000012', 'Bronze',   NULL);

INSERT OR IGNORE INTO PNR_HEADER (pnr_locator, booking_status, created_at)
VALUES
    ('D00001', 'HK', '2025-06-10T10:00:00'),
    ('D00002', 'HK', '2025-06-10T10:05:00'),
    ('D00003', 'HK', '2025-06-10T10:10:00'),
    ('D00004', 'HK', '2025-06-10T10:15:00'),
    ('D00005', 'HK', '2025-06-10T10:20:00'),
    ('D00006', 'HK', '2025-06-10T10:25:00'),
    ('D00007', 'HK', '2025-06-10T10:30:00'),
    ('D00008', 'HK', '2025-06-10T10:35:00'),
    ('D00009', 'HK', '2025-06-10T10:40:00'),
    ('D00010', 'HK', '2025-06-10T10:45:00'),
    ('D00011', 'HK', '2025-06-10T10:50:00'),
    ('D00012', 'HK', '2025-06-10T10:55:00');

INSERT OR IGNORE INTO PASSENGER_SEGMENT (segment_id, pnr_locator, pax_id, flight_num, ticket_num, class_of_service)
VALUES
    ('SEG-D-01', 'D00001', 'PAX-D-01', 'QF500', 'TKT-D-01', 'J'),
    ('SEG-D-02', 'D00002', 'PAX-D-02', 'QF500', 'TKT-D-02', 'J'),
    ('SEG-D-03', 'D00003', 'PAX-D-03', 'QF500', 'TKT-D-03', 'J'),
    ('SEG-D-04', 'D00004', 'PAX-D-04', 'QF500', 'TKT-D-04', 'W'),
    ('SEG-D-05', 'D00005', 'PAX-D-05', 'QF500', 'TKT-D-05', 'W'),
    ('SEG-D-06', 'D00006', 'PAX-D-06', 'QF500', 'TKT-D-06', 'W'),
    ('SEG-D-07', 'D00007', 'PAX-D-07', 'QF500', 'TKT-D-07', 'Y'),
    ('SEG-D-08', 'D00008', 'PAX-D-08', 'QF500', 'TKT-D-08', 'Y'),
    ('SEG-D-09', 'D00009', 'PAX-D-09', 'QF500', 'TKT-D-09', 'Y'),
    ('SEG-D-10', 'D00010', 'PAX-D-10', 'QF500', 'TKT-D-10', 'Y'),
    ('SEG-D-11', 'D00011', 'PAX-D-11', 'QF500', 'TKT-D-11', 'Y'),
    ('SEG-D-12', 'D00012', 'PAX-D-12', 'QF500', 'TKT-D-12', 'Y');

INSERT OR IGNORE INTO IROPS_LOG (irops_id, flight_num, iata_delay_code, triggered_at, resolution_status, is_custom)
VALUES ('IROPS-D-001', 'QF500', 41, '2025-07-14T09:30:00', 'pending', 0);
