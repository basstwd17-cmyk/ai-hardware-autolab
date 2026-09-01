// Gate12: verified GPIO-SPI CODE registers plus PRU-to-DAC1 path.
//
// Configuration (before a run):
//   BBB GPIO-SPI -> four writable/readable 14-bit CODE registers.
// Real-time:
//   BBB PRU SYM.A[1:0] selects one CODE and drives U6 AD9744 D1B[13:0].
//   BBB PRU LDASC.A is forwarded to U6 D1CLK.
//
// SPI mode 0, 24-bit frame, MSB first:
//   write: [0b000000aa, 00|code[13:8], code[7:0]]
//   read:  [0b100000aa, 0x00,          0x00]
//   reply: [0x00,       00|code[13:8], code[7:0]]
//
// Channel B and every unrelated output are deliberately absent.

module gate12_pru_dac1 (
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    input  wire        spi_cs_n,
    output wire        spi_miso,
    input  wire        sym_a0,
    input  wire        sym_a1,
    input  wire        ldac_a,
    output reg  [13:0] d1b,
    output wire        d1clk
);
    reg [13:0] code0;
    reg [13:0] code1;
    reg [13:0] code2;
    reg [13:0] code3;
    reg [23:0] rx_shift;
    reg [15:0] tx_word;
    reg [4:0] bit_count;

    // Zero is the only safe autonomous startup code. The host must explicitly
    // write and read back all four codes before starting the PRU sequence.
    initial begin
        code0 = 14'h0000;
        code1 = 14'h0000;
        code2 = 14'h0000;
        code3 = 14'h0000;
        rx_shift = 24'h000000;
        tx_word = 16'h0000;
        bit_count = 5'd0;
    end

    // Gate11's proven bit ordering is retained, but CS is deliberately not an
    // asynchronous reset. Diamond otherwise promotes CS to the device-wide
    // GSR network. Every host transaction is exactly one 24-clock frame, so
    // the counter returns to zero at the end of each accepted transaction.
    always @(posedge spi_sclk) begin
        if (!spi_cs_n) begin
            rx_shift <= {rx_shift[22:0], spi_mosi};
            if (bit_count == 5'd7) begin
                case ({rx_shift[0], spi_mosi})
                    2'b00: tx_word <= {2'b00, code0};
                    2'b01: tx_word <= {2'b00, code1};
                    2'b10: tx_word <= {2'b00, code2};
                    default: tx_word <= {2'b00, code3};
                endcase
            end
            if (bit_count == 5'd23) begin
                if (!rx_shift[22] && rx_shift[21:17] == 5'b00000) begin
                    case (rx_shift[16:15])
                        2'b00: code0 <= {rx_shift[12:0], spi_mosi};
                        2'b01: code1 <= {rx_shift[12:0], spi_mosi};
                        2'b10: code2 <= {rx_shift[12:0], spi_mosi};
                        default: code3 <= {rx_shift[12:0], spi_mosi};
                    endcase
                end
                bit_count <= 5'd0;
            end else begin
                bit_count <= bit_count + 1'b1;
            end
        end
    end

    assign spi_miso = (!spi_cs_n && bit_count >= 5'd8 && bit_count < 5'd24)
                    ? tx_word[23 - bit_count] : 1'b0;

    // Combinational selection gives the data bus the complete PRU settle
    // interval before the rising D1CLK edge.
    always @* begin
        case ({sym_a1, sym_a0})
            2'b00: d1b = code0;
            2'b01: d1b = code1;
            2'b10: d1b = code2;
            default: d1b = code3;
        endcase
    end

    assign d1clk = ldac_a;
endmodule
