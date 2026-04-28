import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const COMBINED_BUS = new Set(["VT Core", "International"]);

function parseMoney(v: string | number): number {
  if (typeof v === "number") return v;
  return parseFloat(String(v).replace(/[$,]/g, "")) || 0;
}

function parseInt2(v: string | number): number {
  if (typeof v === "number") return v;
  return parseInt(String(v).replace(/,/g, ""), 10) || 0;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  try {
    const body = await req.text();
    const payload = JSON.parse(body);

    // Looker wraps data in { attachment: { data: [...] } }
    const rawData = payload?.attachment?.data ?? payload;
    const rows: Record<string, unknown>[] =
      typeof rawData === "string" ? JSON.parse(rawData) : rawData;

    if (!Array.isArray(rows) || rows.length === 0) {
      console.error("No usable rows. Keys received:", Object.keys(payload));
      return new Response(
        JSON.stringify({ error: "expected array in attachment.data or top-level" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    // Trim whitespace from keys (Looker sometimes pads them)
    const trimmed = rows.map((row) => {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(row)) {
        out[k.trim()] = v;
      }
      return out;
    });

    // Group by week to combine VT Core + International
    const byWeek = new Map<
      string,
      { combined: { count: number; value: number }; profCerts: { count: number; value: number } }
    >();

    for (const row of trimmed) {
      const week = String(row["Record Created Week"] ?? "").trim();
      const bu = String(row["Business"] ?? "").trim();
      if (!week || !bu) continue;

      if (!byWeek.has(week)) {
        byWeek.set(week, {
          combined: { count: 0, value: 0 },
          profCerts: { count: 0, value: 0 },
        });
      }
      const entry = byWeek.get(week)!;
      const count = parseInt2(row["Count"] as string | number);
      const value = parseMoney(row["Conv Value"] as string | number);

      if (COMBINED_BUS.has(bu)) {
        entry.combined.count += count;
        entry.combined.value += value;
      } else if (bu === "Prof Certs") {
        entry.profCerts.count += count;
        entry.profCerts.value += value;
      }
    }

    // Build upsert rows
    const upsertRows: {
      week_start: string;
      business: string;
      client_count: number;
      conv_value: number;
      source: string;
    }[] = [];

    for (const [week, data] of byWeek) {
      if (data.combined.count > 0) {
        for (const bu of ["VT Core", "International"]) {
          upsertRows.push({
            week_start: week,
            business: bu,
            client_count: data.combined.count,
            conv_value: Math.round(data.combined.value * 100) / 100,
            source: "looker:marketing_contribution_conversion_feed",
          });
        }
      }
      if (data.profCerts.count > 0) {
        upsertRows.push({
          week_start: week,
          business: "Prof Certs",
          client_count: data.profCerts.count,
          conv_value: Math.round(data.profCerts.value * 100) / 100,
          source: "looker:marketing_contribution_conversion_feed",
        });
      }
    }

    if (upsertRows.length === 0) {
      return new Response(
        JSON.stringify({ error: "no valid rows after parsing", raw_count: rows.length }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

    // Upsert in batches
    const batchSize = 500;
    let totalUpserted = 0;

    for (let i = 0; i < upsertRows.length; i += batchSize) {
      const batch = upsertRows.slice(i, i + batchSize);
      const { error } = await supabase
        .from("marketing_eltv_actuals")
        .upsert(batch, { onConflict: "week_start,business" });

      if (error) {
        console.error(`Batch error at ${i}:`, error);
        return new Response(
          JSON.stringify({ error: error.message, batch_start: i }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      totalUpserted += batch.length;
    }

    console.log("eLTV ingest complete:", {
      looker_rows: rows.length,
      weeks: byWeek.size,
      rows_upserted: totalUpserted,
    });

    return new Response(
      JSON.stringify({
        success: true,
        looker_rows_received: rows.length,
        weeks_processed: byWeek.size,
        rows_upserted: totalUpserted,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error("ingest-eltv error:", (err as Error).message);
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
});
