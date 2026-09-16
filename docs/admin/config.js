/* Vareska – public backend configuration (Supabase).
 * The anon key is a public value: every client (the app, this panel, the
 * password-reset page) embeds it; row-level security decides what it may do.
 * Keep docs/auth/config.js identical.
 */
window.VareskaConfig = {
  supabaseUrl: 'https://ygogznerwabvlnwpgikx.supabase.co',
  supabaseAnonKey: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlnb2d6bmVyd2Fidmxud3BnaWt4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1MTk3MTIsImV4cCI6MjEwNTA5NTcxMn0.1PIKN3VJbmsOY05kRrTZtXROLl-bqoEH0eY3jSN_pic',
  photoBucket: 'recipe-photos',
  appName: 'Vareska',
};
