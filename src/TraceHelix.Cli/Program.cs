using var cancellation = new CancellationTokenSource();
Console.CancelKeyPress += (_, eventArgs) =>
{
    eventArgs.Cancel = true;
    cancellation.Cancel();
};
return await TraceHelix.Cli.CliProgram.RunAsync(args, Console.Out, Console.Error, cancellation.Token);
