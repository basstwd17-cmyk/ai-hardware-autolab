// Non-DAC diagnostic image for the BBB SPI0 -> MachXO2 path.
//
// The five debug outputs go back to BBB GPIO inputs and latch progressively:
//   alive       configuration is active and the output path works
//   cs_seen     a falling edge reached CPLD SPI.CS
//   sclk_seen   an SCLK rising edge arrived while CS was low
//   frame_done  24 SCLK rising edges completed a frame
//   write_done  the completed frame decoded as a valid CODE0..3 write

module spi_path_diag (
    input  wire spi_sclk,
    input  wire spi_mosi,
    input  wire spi_cs_n,
    output wire spi_miso,
    output wire alive,
    output reg  cs_seen,
    output reg  sclk_seen,
    output reg  frame_done,
    output reg  write_done
);
    reg [13:0] code0;
    reg [13:0] code1;
    reg [13:0] code2;
    reg [13:0] code3;
    reg [23:0] rx_shift;
    reg [15:0] read_word;
    reg [4:0] bit_count;

    // Keep this observable and non-constant so the fitter must honor the
    // reviewed pin location.  CODE1 powers up odd and remains SPI-writable.
    assign alive = code1[0];

    initial begin
        code0 = 14'h0000;
        code1 = 14'h1555;
        code2 = 14'h2aaa;
        code3 = 14'h3fff;
        rx_shift = 24'h0;
        read_word = 16'h0;
        bit_count = 5'h0;
        cs_seen = 1'b0;
        sclk_seen = 1'b0;
        frame_done = 1'b0;
        write_done = 1'b0;
    end

    always @(negedge spi_cs_n) begin
        cs_seen <= 1'b1;
    end

    always @(posedge spi_sclk) begin
        if (!spi_cs_n) begin
            sclk_seen <= 1'b1;
            rx_shift <= {rx_shift[22:0], spi_mosi};

            if (bit_count == 5'd7) begin
                case ({rx_shift[0], spi_mosi})
                    2'b00: read_word <= {2'b00, code0};
                    2'b01: read_word <= {2'b00, code1};
                    2'b10: read_word <= {2'b00, code2};
                    default: read_word <= {2'b00, code3};
                endcase
            end

            if (bit_count == 5'd23) begin
                frame_done <= 1'b1;
                if (!rx_shift[22] && rx_shift[21:17] == 5'b00000) begin
                    case (rx_shift[16:15])
                        2'b00: code0 <= {rx_shift[12:0], spi_mosi};
                        2'b01: code1 <= {rx_shift[12:0], spi_mosi};
                        2'b10: code2 <= {rx_shift[12:0], spi_mosi};
                        default: code3 <= {rx_shift[12:0], spi_mosi};
                    endcase
                    write_done <= 1'b1;
                end
                bit_count <= 5'h0;
            end else begin
                bit_count <= bit_count + 1'b1;
            end
        end
    end

    assign spi_miso = (!spi_cs_n && bit_count >= 5'd8 && bit_count <= 5'd23)
                    ? read_word[23 - bit_count]
                    : 1'b0;
endmodule
