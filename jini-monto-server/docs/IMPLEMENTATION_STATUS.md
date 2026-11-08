# Implementation status

## Implemented in this starter
- FastAPI API with SQLite persistence
- Demo menu and demo staff records
- Order creation with price snapshot
- Cash payment confirmation with denomination breakdown
- Change calculation and audit records
- Attendance upsert with old/new values in audit log
- Basic attendance-linked gross salary estimate
- Basic daily sales/cash summary
- Browser UI for orders, cash, attendance, salary estimate, daily closing and audit history
- Automated smoke tests

## Not implemented / not production-ready
- Production authentication, authorization, password reset and staff PIN security
- Multi-user concurrency testing and PostgreSQL migration
- UPI/card provider integration, signed webhooks, settlement reconciliation
- Refund workflow, discount authorization and credit notes
- Opening float, cash deposits/withdrawals, cash drawer counts and full close approval
- GST/tax-compliant invoice issuance and legal payroll rules
- Cloud sync service, remote dashboard, monitoring and production backups
- Production deployment files / domain / TLS
- Hardware firmware, Modbus, actual dispensing, safety circuits and emergency-stop integration
- Recipe customization, customer profiles, purchase history, reports PDF/Excel
- Clock-in/out, overtime, leave approvals, payroll approval and payslip PDFs

Do not use for real payments, legally relied-upon payroll, or real machine operation.
