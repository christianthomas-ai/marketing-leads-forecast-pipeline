import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  try {
    const body = await req.text();
    const payload = JSON.parse(body);
    const rawData = payload?.attachment?.data;

    if (!rawData) {
      return new Response(
        JSON.stringify({ error: "No data in payload" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    const rows = typeof rawData === "string" ? JSON.parse(rawData) : rawData;

    const trimmedRows = rows.map((row: Record<string, unknown>) => {
      const trimmed: Record<string, unknown> = {};
      for (const [key, val] of Object.entries(row)) {
        trimmed[key.trim()] = val;
      }
      return trimmed;
    });

    const cleanRows = trimmedRows
      .map((row: Record<string, unknown>) => {
        const reportingDate = row["Reporting Date"];
        const reportingWeek = row["Reporting Week"];
        const business = row["Business"];
        const audience = row["Audience (Sales)"];
        const leadSourceGroup = row["Lead Source Group"];
        const adSpend = row["Ad Spend (Total, incl VSX)"] || row["Ad Spend  (Total, incl VSX)"];
        const leadsValid = row["Leads (Valid)"];

        if (!reportingDate || !business) return null;

        return {
          "Reporting Date": reportingDate,
          "Reporting Week": reportingWeek,
          "Business": business,
          "Audience (Sales)": audience,
          "Lead Source Group": leadSourceGroup,
          "Ad Spend  (Total, incl VSX)": adSpend || "$0.00",
          "Leads (Valid)": leadsValid === null || leadsValid === undefined ? 0 : Number(leadsValid),
        };
      })
      .filter(Boolean);

    if (cleanRows.length === 0) {
      return new Response(
        JSON.stringify({ error: "No valid rows to insert" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

    // Wipe existing data
    const { error: deleteError } = await supabase
      .from("leads_weekly_actuals")
      .delete()
      .neq("id", 0);

    if (deleteError) {
      console.error("Delete error:", deleteError);
      return new Response(
        JSON.stringify({ error: deleteError.message }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }

    // Insert fresh data
    const batchSize = 500;
    let totalInserted = 0;

    for (let i = 0; i < cleanRows.length; i += batchSize) {
      const batch = cleanRows.slice(i, i + batchSize);
      const { error } = await supabase
        .from("leads_weekly_actuals")
        .insert(batch);

      if (error) {
        console.error(`Batch error at ${i}:`, error);
        return new Response(
          JSON.stringify({ error: error.message, batch_start: i }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
      totalInserted += batch.length;
    }

    console.log("Leads ingest complete:", { rows_received: rows.length, rows_inserted: totalInserted });

    // Auto-run forecast
    console.log("Running generate_forecast()...");
    const { error: forecastError } = await supabase.rpc("generate_forecast");

    if (forecastError) {
      console.error("Forecast generation error:", forecastError);
      return new Response(
        JSON.stringify({
          success: true,
          rows_received: rows.length,
          rows_inserted: totalInserted,
          forecast: "FAILED",
          forecast_error: forecastError.message,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }

    console.log("Forecast generated successfully.");

    return new Response(
      JSON.stringify({
        success: true,
        rows_received: rows.length,
        rows_inserted: totalInserted,
        forecast: "OK",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );

  } catch (err) {
    console.error("CATCH ERROR:", err.message);
    return new Response(
      JSON.stringify({ error: err.message }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
});