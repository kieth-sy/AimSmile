# AIM Smiles Dental Clinic Management System

## 1. System overview

AIM Smiles is a Django-based clinic system for managing patient accounts, appointments, clinical records, prescriptions, billing, inventory, and public clinic information. Staff use role-specific dashboards and a custom management panel; patients can register, book appointments, and review their records.

The application is organized as Django Model-View-Template apps:

1. A URL configuration routes the request to an application view.
2. The view checks authentication/role access, loads or updates model data, and prepares template context.
3. Forms validate user input, models persist the data, and templates render the page.
4. Shared and page-specific CSS/JavaScript provide presentation and chart behavior.

The standard Django admin app is not installed; administrators use the custom `manage_panel` app.

## 2. Roles and access

| Role | Main capabilities |
| --- | --- |
| Administrator | Clinic dashboard and reports, staff/user management, and management of services, inventory, and reviews. |
| Dentist | Dentist dashboard, assigned appointments, patient and treatment records, and prescriptions. |
| Receptionist | Front-desk dashboard and clinic scheduling, patient, invoice, and inventory workflows. |
| Patient | Patient dashboard, appointment booking and history, own clinical/billing information, profile, and review submission. |

`accounts.User.role` stores the application role. `accounts.decorators.role_required()` checks roles for protected views; its `admin_required` and `staff_required` wrappers define common access groups. Superusers pass role checks. Some views also apply object-level filters—for example, patients see their own appointments and dentists see appointments assigned to them.

After login, `accounts.redirect_dashboard()` sends each role to the correct dashboard. A patient or dentist profile is created by the `clinic` post-save signal when a matching user is saved.

For the detailed `User` fields, properties, and theme classes, see [Account model documentation](./accounts/MODELS.md).

## 3. Main application areas

### Public website

The `pages` app renders the home, services, about, and contact pages. The homepage uses active `Service` records and public `Review` records. Public routes are defined in [`pages/urls.py`](./pages/urls.py); the shared public shell is [`templates/public_base.html`](./templates/public_base.html).

### Accounts and profiles

The `accounts` app provides sign-up, login/logout, role-based dashboard redirects, profile editing, and password setup for any previously issued setup links. Administrators set an initial password when creating a user; the application does not send account emails.

### Clinic operations

The `clinic` app contains the dashboards and workflows for appointments, patients, tooth records, treatments, prescriptions, invoices, payments, inventory, and reviews.

The appointment page currently uses a custom weekly time-grid rendered by `appointment_list.html` and its inline JavaScript. Previous/next week and Today controls move the displayed week; selecting an appointment fills the details panel. The page is not currently using FullCalendar.

### Administration and analytics

The `manage_panel` app provides an administrator dashboard, reports, staff account management, and generic resource CRUD. Its resource registry currently exposes Services, Inventory, and Reviews. New accounts require an administrator-provided initial password.

The reports page includes revenue and patient-growth charts, appointment status, treatment mix, and a Top Services polar-area chart. Revenue includes confirmed payments only: the KPI shows the all-time collected total, and the trend groups payments by `paid_on` month. Patients can submit a downpayment request for 50% of the final invoice total, less confirmed payments; staff must verify it before it changes the balance or revenue. Top Services counts treatment records by service; when there are no treatment records, it uses appointment bookings as its data source. The UI displays an empty state when neither source has service data.

Administrators can open **Activity Logs** from the sidebar at `/manage/activity-logs/`. The page records successful and failed sign-ins, sign-outs, account and resource changes, appointment changes, and payment/invoice actions. Entries include the actor where known, event time, event summary, target record reference, and remote IP; passwords, one-time codes, and submitted form contents are not stored. The list is searchable, filterable by event, and paginated.

### Assistant tools

The `aiassistant` app provides a symptom checker, DentaBot, a cost estimator, and a role-protected hub. The symptom checker, cost estimator, and no-show score use local rules and heuristics in [`aiassistant/engine.py`](./aiassistant/engine.py):

- `symptom_triage()` matches symptom keywords to a possible condition, urgency, suggested service, and general advice.
- `no_show_risk()` calculates a heuristic appointment risk score; `risk_band()` maps the score to Low, Medium, or High.
- `estimate_cost()` applies a complexity multiplier, tooth count, tax, and optional estimated insurance coverage.

DentaBot uses local programmed replies in `engine.chatbot_response()`; it does not call an external AI provider, require an API key, or save chat history. It answers a limited set of common questions about appointments, listed services and prices, payments, and contacting the clinic. Unknown hours, prices, and location details are not invented. The public endpoint has a per-IP request limit.

The local assistant is informational only and does not diagnose or replace care from a licensed dentist. Its replies are rule-based rather than generative AI.

## 4. Data model summary

The models live in [`clinic/models.py`](./clinic/models.py), except for the custom account model in [`accounts/models.py`](./accounts/models.py).

| Model | Purpose and key attributes |
| --- | --- |
| `User` | Django account plus `role`, phone, gender, date of birth, address, avatar, and bio. Provides display name, initials, role checks, and theme metadata. |
| `IdentityVerification` | Consented identity-check metadata, keyed ID digest, and encrypted face template for duplicate checks. |
| `SignupDuplicateReport` | Possible duplicate-signup record with optional candidate, matched-user, and reviewer links. |
| `PhoneVerification` | Hashed one-time phone code with expiry and attempt state. |
| `DoctorProfile` | Dentist-specific specialization, license, experience, consultation fee, available days, rating, and availability. One-to-one with a user. |
| `PatientProfile` | Patient code, blood group, emergency contact, allergies, medical history, and registration date. One-to-one with a user; generates a patient code if blank. |
| `Service` | Service name, description, price, duration, icon/image, and active flag. |
| `Appointment` | Patient, optional dentist/service, date/time, status, reason/notes, no-show risk, and creation timestamp. Status choices: pending, confirmed, completed, cancelled, and no-show. |
| `ToothRecord` | Patient, universal tooth number (1–32), condition, note, and last update. A patient cannot have duplicate records for the same tooth. |
| `TreatmentRecord` | Patient, optional dentist/appointment/service, optional tooth number, description, cost, date, and completion flag. |
| `Prescription` | Patient, optional dentist/appointment, date, diagnosis, and notes. |
| `PrescriptionItem` | Medicine, dosage, frequency, duration, and instructions; belongs to a prescription. |
| `Invoice` | Patient, optional appointment, issue/due dates, tax, discount, status, and notes. Provides subtotal, tax, total, amount paid, and balance calculations. |
| `InvoiceItem` | Description, quantity, unit price, and calculated line total; belongs to an invoice. |
| `Payment` | Invoice, amount, payment method, date, optional reference, and pending/confirmed/rejected verification status. Methods include cash, card, UPI, and insurance. |
| `ActivityLog` | Audit event with actor snapshot, event type, safe summary, optional target reference, remote IP, and timestamp. |
| `ContactMessage` | Public inquiry and administrator response/status, with an optional replying user. |
| `ClinicGoogleCalendarConnection` | Optional clinic-scoped calendar credentials and connecting-user reference. |
| `InventoryItem` | Name, category, quantity/unit, reorder level, unit price, supplier, and icon. `is_low` is true when quantity is at or below the reorder level. |
| `Review` | Patient, optional dentist, rating, comment, creation date, and public visibility flag. |

### Important relationships

- One patient can have many appointments, treatments, tooth records, prescriptions, invoices, and reviews.
- An appointment may be linked to a service and dentist; those links can be empty.
- A treatment can reference its appointment, service, dentist, and tooth; some of these relations are nullable in the database.
- A prescription contains one or more prescription items.
- An invoice contains invoice items and can have multiple payments.

### Choices and calculated properties

- Appointment status: Pending, Confirmed, Completed, Cancelled, or No Show.
- Tooth condition: Healthy, Cavity, Filled, Root Canal, Crown, or Missing.
- Invoice status: Unpaid, Partially Paid, or Paid. `recalculate_status()` updates it from recorded payments.
- `Appointment.is_upcoming` checks the date and pending/confirmed status; `Appointment.status_color` maps status to a UI color.
- `ToothRecord.condition_color` maps the selected condition to a chart color.
- `Invoice.subtotal`, `tax_amount`, `total`, `amount_paid`, and `balance` are calculated from line items, tax, discount, and payments.
- `InventoryItem.is_low` compares quantity with its reorder level. Patient and invoice numbers are generated when their model records are first saved without a number.

## 5. Main workflows

### Patient registration and appointment

1. A visitor creates an account; self-registration creates a patient account.
2. The profile signal creates the matching `PatientProfile`.
3. The patient books a service, date, and time. New patient bookings start with Pending status.
4. Staff manage the appointment status and can access appointment details according to role.
5. The appointment may later be linked to treatment and billing records.

### Treatment and tooth chart

1. Staff select a patient and record a treatment, optionally linking it to an appointment, service, and tooth.
2. Tooth records store one condition per numbered tooth for each patient.
3. Treatment records contribute to the treatment mix and Top Services analytics.

### Prescription and billing

1. A prescription stores diagnosis/notes and related medicine items.
2. An invoice stores line items and applies its tax and discount values.
3. Staff record confirmed payments, while patient-submitted 50% downpayments remain pending until staff verifies or rejects them. Only confirmed payments affect the invoice balance and revenue.

### Admin-created accounts

1. An administrator creates a user in the management panel.
2. The administrator sets the account's initial password.
3. The administrator shares the login details with the recipient through a secure channel.
4. The recipient signs in through the normal login page.

## 6. Analytics and charts

Reusable data aggregation functions are in [`clinic/analytics.py`](./clinic/analytics.py):

| Function | What it summarizes |
| --- | --- |
| `revenue_by_month()` | Payment totals by month for the latest 12 months. |
| `appointments_by_status()` | Appointment counts for every defined status. |
| `appointments_per_month()` | Appointment counts by month for the latest 12 months. |
| `treatment_distribution()` | Up to six treatment-record groups by service. |
| `patient_growth()` | Cumulative patient registrations by month for the latest 12 months. |
| `admin_kpis()` | Patient/dentist/appointment/treatment totals, today's appointments, revenue, and outstanding invoice balance. |

Chart rendering helpers are in [`static/js/app.js`](./static/js/app.js). Chart.js is loaded by the shared base template. Most dashboard and report charts use neumorphic inset wells, soft raised shadows on chart marks, and theme-aware labels/tooltips. Revenue Trend and Patient Growth retain their plain chart presentation. The Top Services chart uses a polar-area plot with a right-side legend. The appointment status chart uses a visible color key on the administrator dashboard: Pending (yellow), Confirmed (blue), Completed (green), Cancelled (orange), and No Show (red).

## 7. URL reference

| Path | Purpose |
| --- | --- |
| `/` | Public homepage |
| `/services/`, `/about/`, `/contact/` | Public information pages |
| `/accounts/login/`, `/accounts/logout/`, `/accounts/signup/` | Authentication |
| `/accounts/go/` | Redirect to the signed-in user's dashboard |
| `/accounts/profile/` | Profile editing |
| `/app/dentist/`, `/app/reception/`, `/app/me/` | Dentist, receptionist, and patient dashboards |
| `/app/appointments/` | Appointment schedule and records |
| `/app/patients/` | Patient records |
| `/app/treatments/`, `/app/prescriptions/` | Treatment and prescription workflows |
| `/app/invoices/`, `/app/inventory/` | Billing and inventory |
| `/ai/symptom-checker/`, `/ai/chatbot/`, `/ai/cost-estimator/` | Assistant tools |
| `/ai/chatbot/api/` | POST endpoint used by the chatbot UI |
| `/manage/`, `/manage/reports/`, `/manage/activity-logs/` | Administrator dashboard, analytics, and activity logs |
| `/manage/users/`, `/manage/services/`, `/manage/inventory/`, `/manage/reviews/` | Admin account/resource management |

Route definitions are split across [`dental_clinic/urls.py`](./dental_clinic/urls.py) and each app's `urls.py`.

## 8. Templates, CSS, and JavaScript

- [`templates/base.html`](./templates/base.html) loads shared Bootstrap, Bootstrap Icons, Chart.js, and application assets.
- [`templates/public_base.html`](./templates/public_base.html) is the public-site shell.
- [`templates/app_base.html`](./templates/app_base.html) is the signed-in application shell with role-aware navigation.
- [`static/css/app.css`](./static/css/app.css) contains shared layout, typography, and reusable components.
- [`static/css/aimsmile.css`](./static/css/aimsmile.css) contains the AimSmile dashboard/neumorphic styling and theme behavior.
- `static/css/pages/` contains styles scoped to specific pages, including the dashboard, appointment list, and homepage.
- [`static/js/app.js`](./static/js/app.js) contains shared dashboard interactions and chart helpers such as line, bar, doughnut, appointment-status, and pie charts.

Role theme metadata uses root classes such as `theme-admin`, `theme-dentist`, `theme-receptionist`, and `theme-patient`. The current shared CSS uses a common blue palette; see [Account model documentation](./accounts/MODELS.md) for details.

## 9. Development setup

From the project root on Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. If activation is blocked, the virtual environment can also be used directly, for example `.\venv\Scripts\python.exe manage.py check`.

The default database is MySQL, configured through `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT` in `.env`; `DB_ENGINE=sqlite` selects the local SQLite database for development or data export. The MySQL driver is PyMySQL. Uploaded media is stored under `media/`, the timezone is `Asia/Kolkata`, and the clinic currency is PHP. Do not commit `.env` or real credentials.

## 10. Production and safety notes

The checked-in settings are development-oriented. Before deployment:

- set `DEBUG=False` and restrict `ALLOWED_HOSTS`;
- load a strong `SECRET_KEY` from the environment rather than source code;
- configure a production database, HTTPS, static/media hosting, and email delivery;
- set the timezone to the clinic's actual location;
- change demo credentials and use a strong password policy;
- protect patient data with appropriate access controls, backups, and retention procedures.

Demo data and placeholder contact information are for local testing only. Assistant responses are local, rule-based, informational, and must not be used as a clinical diagnosis.

## System diagrams

The current editable diagrams are maintained in [`report/diagrams/`](./report/diagrams/):

- [Context diagram](./report/diagrams/12_context_diagram.svg)
- [Level 1 data-flow diagram](./report/diagrams/11_dfd_level1_clean.svg)
- [Entity-relationship diagram](./report/diagrams/03_er_diagram.svg), with editable Mermaid source at [03_er_diagram.mmd](./report/diagrams/03_er_diagram.mmd)
- [Input–Process–Output framework](./report/diagrams/13_conceptual_framework_ipo.svg)

The diagrams include the ActivityLog model and administrator activity/login workflow. The system diagrams describe implemented features and optional integrations; they do not imply that every external provider is enabled. DentaBot is rule-based and does not send chat messages to an external AI provider.

## Related documentation

- [Project overview and installation](./README.md)
- [Account models, role attributes, and theme classes](./accounts/MODELS.md)
