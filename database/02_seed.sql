-- ============================================================
-- ONE CLICK HIRE  -  Seed data (Phase 1)
-- Real Amnex structure: group companies, business units, bands,
-- plus starter config (approval chains, email templates, a
-- feedback form, and a sample requisition with rounds).
-- Run AFTER 01_schema.sql.
-- ============================================================

-- ---------- GROUP COMPANIES ----------
INSERT INTO group_company (name, domain) VALUES
  ('Amnex',        'Parent company'),
  ('Maxsapient',   'Aviation'),
  ('Andpayments',  'Payments'),
  ('Immensystech', 'IOT'),
  ('ACAI',         'AI'),
  ('Metafusion',   'Technology');

-- ---------- BUSINESS UNITS (under Amnex parent) ----------
INSERT INTO business_unit (company_id, name)
SELECT id, bu.name
FROM group_company gc
CROSS JOIN (VALUES
  ('Corporate'), ('Traffic'), ('Mobility'), ('Integrated'),
  ('R&U'), ('Sports'), ('Agriculture'), ('Datafabrics')
) AS bu(name)
WHERE gc.name = 'Amnex';

-- ---------- BANDS (lowest rank 1 -> highest rank 13) ----------
INSERT INTO band (code, rank, description) VALUES
  ('5',  1,  'Entry / blue-collar / fresher'),
  ('4C', 2,  'Junior'),
  ('4B', 3,  'Junior'),
  ('4A', 4,  'Associate'),
  ('3D', 5,  'Executive'),
  ('3C', 6,  'Executive'),
  ('3B', 7,  'Senior executive'),
  ('3A', 8,  'Lead'),
  ('2C', 9,  'Manager'),
  ('2B', 10, 'Senior manager'),
  ('2A', 11, 'AGM'),
  ('1B', 12, 'GM / VP'),
  ('1A', 13, 'Senior leadership');

-- ---------- USERS (sample; replace with real team) ----------
INSERT INTO app_user (full_name, email, role) VALUES
  ('TA Admin',        'ta.admin@amnex.com',     'admin'),
  ('TA Manager',      'ta.manager@amnex.com',   'ta_manager'),
  ('Recruiter One',   'recruiter1@amnex.com',   'recruiter'),
  ('Recruiter Two',   'recruiter2@amnex.com',   'recruiter');

-- ---------- APPROVAL CHAINS (per band band group) ----------
-- Junior bands: BU head only. Senior bands: BU head + director.
-- Edit approver_steps any time to change who signs off.
INSERT INTO approval_chain (band_id, name, approver_steps)
SELECT b.id,
       'Junior chain (' || b.code || ')',
       '[{"step":1,"role":"bu_head"}]'::jsonb
FROM band b WHERE b.rank <= 5;

INSERT INTO approval_chain (band_id, name, approver_steps)
SELECT b.id,
       'Senior chain (' || b.code || ')',
       '[{"step":1,"role":"bu_head"},{"step":2,"role":"director"}]'::jsonb
FROM band b WHERE b.rank > 5;

-- ---------- FEEDBACK FORM (default panel scorecard) ----------
INSERT INTO feedback_form (name, schema, created_by)
SELECT 'Default panel scorecard',
       '[
         {"key":"technical","label":"Technical skills","type":"rating_5"},
         {"key":"communication","label":"Communication","type":"rating_5"},
         {"key":"culture_fit","label":"Culture fit","type":"rating_5"},
         {"key":"comments","label":"Comments","type":"text"}
       ]'::jsonb,
       u.id
FROM app_user u WHERE u.email = 'ta.admin@amnex.com';

-- ---------- EMAIL TEMPLATES (customizable) ----------
INSERT INTO email_template (name, subject, body, category, created_by)
SELECT t.name, t.subject, t.body, t.category, u.id
FROM app_user u
CROSS JOIN (VALUES
  ('Interview invite (candidate)',
   'Interview scheduled: {{job_title}} at Amnex',
   'Dear {{candidate_name}},

Your interview for {{job_title}} is scheduled on {{interview_time}}.
Meeting link: {{meet_link}}

Regards,
Amnex Talent Acquisition', 'candidate'),
  ('Panel notification',
   'Interview assigned: {{candidate_name}} for {{job_title}}',
   'Hi {{panel_name}},

You are scheduled to interview {{candidate_name}} for {{job_title}} on {{interview_time}}.
Link: {{meet_link}}', 'panel')
) AS t(name, subject, body, category)
WHERE u.email = 'ta.admin@amnex.com';

-- ---------- SAMPLE REQUISITION + ROUNDS (demonstration) ----------
WITH req AS (
  INSERT INTO requisition
    (title, bu_id, band_id, roll_type, key_skills, min_experience,
     budgeted_ctc, openings, fiscal_year, status, opened_at)
  SELECT 'Backend Engineer',
         bu.id, b.id, 'on_roll',
         ARRAY['Python','PostgreSQL','REST API','GCP'],
         3, 1800000, 1, 'FY25-26', 'open', now()
  FROM business_unit bu, band b
  WHERE bu.name = 'Datafabrics' AND b.code = '3B'
  LIMIT 1
  RETURNING id
)
INSERT INTO round_config (requisition_id, sequence, name, round_type, is_auto)
SELECT req.id, r.seq, r.name, r.rtype, r.auto
FROM req
CROSS JOIN (VALUES
  (1, 'AI screening interview', 'bot_interview', TRUE),
  (2, 'Technical round',        'panel',         FALSE),
  (3, 'HR discussion',          'hr',            FALSE)
) AS r(seq, name, rtype, auto);
