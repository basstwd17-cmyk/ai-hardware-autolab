`timescale 1ns/1ps

module rtase_dac_spi_tb;
    reg sym_a0 = 0;
    reg sym_a1 = 0;
    reg ldac_a = 0;
    reg spi_sclk = 0;
    reg spi_mosi = 0;
    reg spi_cs_n = 1;
    wire spi_miso;
    wire [13:0] d1b;
    wire d1clk;

    reg [23:0] rx;

    rtase_dac_spi dut (
        .sym_a0(sym_a0), .sym_a1(sym_a1), .ldac_a(ldac_a),
        .spi_sclk(spi_sclk), .spi_mosi(spi_mosi), .spi_cs_n(spi_cs_n),
        .spi_miso(spi_miso), .d1b(d1b), .d1clk(d1clk)
    );

    task spi_transfer;
        input [23:0] tx;
        output [23:0] result;
        integer bit_index;
        begin
            result = 24'h0;
            spi_cs_n = 0;
            for (bit_index = 23; bit_index >= 0; bit_index = bit_index - 1) begin
                spi_mosi = tx[bit_index];
                #5;
                result[bit_index] = spi_miso;
                spi_sclk = 1;
                #5;
                spi_sclk = 0;
            end
            #5;
            spi_cs_n = 1;
            #5;
        end
    endtask

    task expect_read;
        input [1:0] address;
        input [13:0] expected;
        begin
            spi_transfer({6'b100000, address, 16'h0000}, rx);
            if (rx[13:0] !== expected) begin
                $display("FAIL read CODE%0d: expected=%04h observed=%04h raw=%06h",
                         address, expected, rx[13:0], rx);
                $fatal(1);
            end
        end
    endtask

    task write_code;
        input [1:0] address;
        input [13:0] value;
        begin
            spi_transfer({6'b000000, address, 2'b00, value}, rx);
        end
    endtask

    initial begin
        #20;
        expect_read(0, 14'h0000);
        expect_read(1, 14'h1555);
        expect_read(2, 14'h2aaa);
        expect_read(3, 14'h3fff);

        write_code(0, 14'h0001);
        write_code(1, 14'h0123);
        write_code(2, 14'h2345);
        write_code(3, 14'h3ffe);
        expect_read(0, 14'h0001);
        expect_read(1, 14'h0123);
        expect_read(2, 14'h2345);
        expect_read(3, 14'h3ffe);

        $display("PASS SPI mode-0 initial values and write/readback");
        $finish;
    end
endmodule
