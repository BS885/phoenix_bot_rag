# צ'אטבוט הפניקס (RAG על תוכן ציבורי של www.fnx.co.il)

הבוט עונה רק מתוך תוכן ציבורי של האתר, עם קישור למקור בכל תשובה. אין ייעוץ אישי. הוראות עבודה: [CLAUDE.md](CLAUDE.md), ארכיטקטורה: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), החלטות: [docs/decisions.md](docs/decisions.md).

## מגבלות ידועות
- **עמודי `/SPF` (iframe) אינם נאספים ולא עוקפים אותם.** `robots.txt` של האתר אוסר `Disallow: /SPF`. 142 עמודים (בעיקר פנסיה וגמל: דוחות, הוצאות ישירות, תשואות, הסדרי תביעות) ריקים באינדקס, והבוט יפנה לעמוד המקור.
- **תוכן שנטען בצד לקוח (JavaScript) אינו נאסף** (למשל `car_information/tips`, `whats-new`).
- **שמונה כתובות בעברית תחת `claims-health-insurance` (ב-sitemap) מחזירות 404 באתר ולכן אינן נאספות:** `/claims-customer-service/claims-health-insurance/אישור-לניתוח-עתידי/`; `/claims-customer-service/claims-health-insurance/קבלת-החזר-על-הוצאות-רפואיות-שאינן-באשפוז/`; `/claims-customer-service/claims-health-insurance/קבלת-החזר-על-התייעצות-עם-רופא-מומחה/`; `/claims-customer-service/claims-health-insurance/קבלת-החזר-על-טיפולי-פיזיותרפיה/`; `/claims-customer-service/claims-health-insurance/קבלת-החזר-על-טיפולים-אלטרנטיביים/`; `/claims-customer-service/claims-health-insurance/קבלת-החזר-על-ניתוח/`; `/claims-customer-service/claims-health-insurance/קבלת-החזר-על-תרופה-שאינה-כלולה-בסל-הבריאות/`; `/claims-customer-service/claims-health-insurance/תביעה-בביטוח-מחלות-קשות/`.
- **מחוץ להיקף:** אנגלית, משקיעים, קריירה, תביעות ייצוגיות ותנאי שימוש.
- פירוט מלא: [docs/decisions.md](docs/decisions.md) (סעיף "מגבלות מוכרות").

(המשך README, כולל הרצה ורענון, בשלב 8.)
